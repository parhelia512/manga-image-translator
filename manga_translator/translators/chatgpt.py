import re
import os
import asyncio
import time
import string
from typing import List, Dict
from rich.console import Console  
from rich.panel import Panel
from .. import manga_translator
from .config_gpt import ConfigGPT
from .common import CommonTranslator, MissingAPIKeyException, VALID_LANGUAGES
from .keys import OPENAI_API_KEY, OPENAI_HTTP_PROXY, OPENAI_API_BASE, OPENAI_MODEL, OPENAI_GLOSSARY_PATH

try:
    import openai
except ImportError:
    openai = None


class OpenAITranslator(ConfigGPT, CommonTranslator):
    _LANGUAGE_CODE_MAP = VALID_LANGUAGES
    
    # 类级别的标志，用于跟踪是否已经显示过术语表警告
    _glossary_warning_shown = False

    # ---- 关键参数 ----
    _MAX_REQUESTS_PER_MINUTE = 0
    _TIMEOUT = 999                # 每次请求的超时时间
    _RETRY_ATTEMPTS = 2          # 对同一个批次的最大整体重试次数
    _TIMEOUT_RETRY_ATTEMPTS = 3  # 请求因超时被取消后，最大尝试次数
    _RATELIMIT_RETRY_ATTEMPTS = 3# 遇到 429 等限流时的最大尝试次数
    _MAX_SPLIT_ATTEMPTS = 3      # 递归拆分批次的最大层数
    _MAX_TOKENS = 8192           # prompt+completion 的最大 token (可按模型类型调整)

    def __init__(self, check_openai_key=True):
        # ConfigGPT 的初始化
        _CONFIG_KEY = 'chatgpt.' + OPENAI_MODEL
        ConfigGPT.__init__(self, config_key=_CONFIG_KEY)
        CommonTranslator.__init__(self)

        if not OPENAI_API_KEY and check_openai_key:
            raise MissingAPIKeyException('OPENAI_API_KEY environment variable required')

        # 根据代理与基础URL等参数实例化 openai.AsyncOpenAI 客户端
        client_args = {
            "api_key": OPENAI_API_KEY,
            "base_url": OPENAI_API_BASE
        }
        if OPENAI_HTTP_PROXY:
            from httpx import AsyncClient
            client_args["http_client"] = AsyncClient(proxies={
                "all://*openai.com": f"http://{OPENAI_HTTP_PROXY}"
            })

        self.client = openai.AsyncOpenAI(**client_args)
        self.token_count = 0
        self.token_count_last = 0
        self._last_request_ts = 0
        
        # 初始化术语表相关属性
        self.dict_path = OPENAI_GLOSSARY_PATH
        self.glossary_entries = {}
        
        # 检查用户是否明确设置了glossary环境变量
        user_set_glossary = os.getenv('OPENAI_GLOSSARY_PATH') is not None
        
        if os.path.exists(self.dict_path):
            self.glossary_entries = self.load_glossary(self.dict_path)
        elif user_set_glossary:
            # 只有在用户明确设置了环境变量时才显示警告
            if not OpenAITranslator._glossary_warning_shown:
                self.logger.warning(f"The glossary file does not exist: {self.dict_path}")
                OpenAITranslator._glossary_warning_shown = True

        # 添加 rich 的 Console 对象  
        if hasattr(manga_translator, '_global_console') and manga_translator._global_console:
            self.console = manga_translator._global_console
        else:
            self.console = Console()  
        self.prev_context = ""
        # 可选的回退模型（通过环境变量 OPENAI_FALLBACK_MODEL 指定）
        self._fallback_model = os.getenv("OPENAI_FALLBACK_MODEL")

    def set_prev_context(self, text: str = ""):
        self.prev_context = text or ""     

    def parse_args(self, args: CommonTranslator):
        """如果你有外部参数要解析，可在此对 self.config 做更新"""
        self.config = args.chatgpt_config

    async def _ratelimit_sleep(self):
        """
        在请求前先做一次简单的节流 (如果 _MAX_REQUESTS_PER_MINUTE > 0)。
        针对并发请求进行优化。
        """
        if self._MAX_REQUESTS_PER_MINUTE > 0:
            now = time.time()
            delay = 60.0 / self._MAX_REQUESTS_PER_MINUTE
            elapsed = now - self._last_request_ts
            
            # 为并发请求添加额外的随机延迟，避免同时请求
            # Add extra random delay for concurrent requests to avoid simultaneous requests
            import random
            concurrent_jitter = random.uniform(0.1, 0.5)  # 100-500ms的随机延迟
            
            total_delay = delay + concurrent_jitter
            if elapsed < total_delay:
                await asyncio.sleep(total_delay - elapsed)
            self._last_request_ts = time.time()

    def _assemble_prompts(self, from_lang: str, to_lang: str, queries: List[str]):
        """
        原脚本中用来把多个 query 组装到一个 Prompt。
        同时可以做长度控制，如果过长就切分成多个 prompt。
        这里演示一个简单的 chunk 逻辑：
          - 根据字符长度 roughly 判断
          - 也可以用更准确的 tokens 估算
        """

        lang_name = self._LANGUAGE_CODE_MAP.get(to_lang, to_lang) if to_lang in self._LANGUAGE_CODE_MAP else to_lang
        
        MAX_CHAR_PER_PROMPT = self._MAX_TOKENS * 4  # 粗略: 1 token ~ 4 chars
        chunk_queries = []
        current_length = 0
        batch = []

        for q in queries:
            # +10 给一些余量，比如加上 <|1|> 的标记等
            if current_length + len(q) + 10 > MAX_CHAR_PER_PROMPT and batch:
                # 输出当前 batch
                chunk_queries.append(batch)
                batch = []
                current_length = 0
            batch.append(q)
            current_length += len(q) + 10
        if batch:
            chunk_queries.append(batch)

        # 逐个批次生成 prompt
        for this_batch in chunk_queries:
            prompt = ""
            if self.include_template:
                prompt = self.prompt_template.format(to_lang=lang_name)
            # 加上分行内容
            for i, query in enumerate(this_batch):
                prompt += f"\n<|{i+1}|>{query}"
            yield prompt.lstrip(), len(this_batch)

    async def _translate(self, from_lang: str, to_lang: str, queries: List[str]) -> List[str]:
        """
        核心翻译逻辑：
            1. 把 queries 拆成多个 prompt 批次
            2. 对每个批次调用 translate_batch，并将结果写回 translations
        """
        translations = [''] * len(queries)
        # 记录当前处理到 queries 列表的哪个位置
        idx_offset = 0

        # 分批处理
        for prompt, batch_size in self._assemble_prompts(from_lang, to_lang, queries):
            # 实际要翻译的子列表
            batch_queries = queries[idx_offset : idx_offset + batch_size]
            indices = list(range(idx_offset, idx_offset + batch_size))

            # 执行翻译
            success, partial_results = await self._translate_batch(
                from_lang, to_lang, batch_queries, indices, prompt, split_level=0
            )
            # 将结果写入 translations
            for i, r in zip(indices, partial_results):
                translations[i] = r

            idx_offset += batch_size

        return translations

    async def _try_fallback_model(self, to_lang: str, prompt: str, batch_queries: List[str]) -> tuple[bool, List[str]]:
        """
        尝试使用回退模型进行翻译，默认重试3次
        Returns: (success: bool, results: List[str])
        """
        if not self._fallback_model:
            return False, []
            
        fallback_max_attempts = 2  # 默认重试2次（总共3次请求）
        
        for attempt in range(fallback_max_attempts + 1):  # +1 for initial attempt
            if attempt == 0:
                self.logger.warning(f"Trying fallback model '{self._fallback_model}' (request {attempt+1}/3)")
            else:
                self.logger.warning(f"Trying fallback model '{self._fallback_model}' (retry {attempt}/2, request {attempt+1}/3)")
            
            # 禁用译后检测
            try:
                import inspect
                for st in inspect.stack():
                    cfg = st.frame.f_locals.get("config")
                    if cfg and hasattr(cfg, "translator"):
                        cfg.translator.enable_post_translation_check = False
                        break
            except Exception:
                pass

            from importlib import import_module
            keys_mod = import_module("manga_translator.translators.keys")
            original_model_const = getattr(keys_mod, "OPENAI_MODEL", None)

            try:
                # 临时替换常量，使 _request_with_retry 使用回退模型
                setattr(keys_mod, "OPENAI_MODEL", self._fallback_model)

                # 若当前处于 ChatGPT2StageTranslator 第二阶段，需要同步切换 stage2_model
                orig_stage2 = getattr(self, "stage2_model", None)
                if getattr(self, "_is_stage2_translation", False) and hasattr(self, "stage2_model"):
                    self.stage2_model = self._fallback_model

                # 关闭 stage2 标志，强制 _request_translation 走 OPENAI_MODEL
                orig_stage_flag = getattr(self, "_is_stage2_translation", False)
                try:
                    if orig_stage_flag:
                        self._is_stage2_translation = False
                    response_text_fb = await self._request_with_retry(to_lang, prompt)
                finally:
                    if orig_stage_flag:
                        self._is_stage2_translation = orig_stage_flag

                fb_translations = [t.strip() for t in re.split(r'<\|\d+\|>', response_text_fb)]
                if fb_translations and not fb_translations[0]:
                    fb_translations = fb_translations[1:]

                # 检查 fallback 模型是否提供了有效的翻译
                if len(fb_translations) != len(batch_queries):
                    self.logger.warning(f"Fallback output count mismatch: expected {len(batch_queries)}, got {len(fb_translations)}. Fallback failed.")
                    continue  # 继续重试而不是返回成功

                # 检查是否所有翻译都是空的或与原文相同
                valid_translations = 0
                for i, txt in enumerate(fb_translations):
                    if txt and txt.strip() and txt.strip() != batch_queries[i].strip():
                        valid_translations += 1

                if valid_translations == 0:
                    self.logger.warning("Fallback model returned no valid translations (all empty or same as original). Fallback failed.")
                    continue  # 继续重试而不是返回成功

                result_list = []
                for i, txt in enumerate(fb_translations):
                    result_list.append(txt if txt else batch_queries[i])

                self.logger.info(f"Fallback model succeeded on request {attempt+1} with {valid_translations}/{len(batch_queries)} valid translations")
                return True, result_list

            except Exception as fb_err:
                if attempt == 0:
                    self.logger.warning(f"Fallback model request {attempt+1}/3 failed: {fb_err}")
                else:
                    self.logger.warning(f"Fallback model retry {attempt}/2 (request {attempt+1}/3) failed: {fb_err}")
                if attempt < fallback_max_attempts:
                    await asyncio.sleep(1)  # 重试前等待1秒
                else:
                    self.logger.error(f"All fallback model requests failed")

            finally:
                # 恢复常量与 stage2_model
                if original_model_const is not None:
                    setattr(keys_mod, "OPENAI_MODEL", original_model_const)
                if getattr(self, "_is_stage2_translation", False) and hasattr(self, "stage2_model") and orig_stage2 is not None:
                    self.stage2_model = orig_stage2

        return False, []

    async def _translate_batch(  
        self,  
        from_lang: str,  
        to_lang: str,  
        batch_queries: List[str],  
        batch_indices: List[int],  
        prompt: str,  
        split_level: int = 0  
    ):  
        """  
        尝试翻译 batch_queries。若失败或返回不完整，则进一步拆分。  
        Attempt to translate batch_queries. If failed or incomplete, further split the batch.  
        
        :param from_lang: 源语言 / Source language  
        :param to_lang: 目标语言 / Target language  
        :param batch_queries: 需要翻译的文本列表 / List of texts to be translated  
        :param batch_indices: 批量查询的索引列表 / List of indices for batch queries  
        :param prompt: 发送给翻译服务的提示文本 / Prompt text sent to translation service  
        :param split_level: 当前拆分级别，用于控制递归深度 / Current split level for controlling recursion depth  
        :return: (bool 是否成功, List[str] 对应每个 query 的翻译结果)  
                 (bool success or not, List[str] translation results corresponding to each query)  
        """  
        # 初始化结果列表，与输入查询数量相同  
        # Initialize result list with the same length as input queries  
        partial_results = [''] * len(batch_queries)  
        # 初始化 response_text 变量，避免 UnboundLocalError
        # Initialize response_text variable to avoid UnboundLocalError
        response_text = ""
        
        # 如果没有查询就直接返回  
        # If no queries, return immediately  
        if not batch_queries:  
            return True, partial_results  

        # 进行 _RETRY_ATTEMPTS 次重试  
        # Retry for _RETRY_ATTEMPTS times  
        # 确保至少尝试一次，即使 _RETRY_ATTEMPTS = 0
        # Ensure at least one attempt, even if _RETRY_ATTEMPTS = 0
        max_attempts = max(1, self._RETRY_ATTEMPTS + 1)
        for attempt in range(max_attempts):  
            try:  
                # 发起请求  
                # Send request  
                response_text = await self._request_with_retry(to_lang, prompt)  

                # 解析响应
                # Parse response
                new_translations = re.split(r'<\|\d+\|>', response_text)
                merged_single_query = False

                # 立即清理每个翻译文本的前后空格
                # Immediately clean leading and trailing whitespace from each translation text
                new_translations = [t.strip() for t in new_translations]

                if new_translations and not new_translations[0].strip():
                    new_translations = new_translations[1:]
                
                # 单查询多段响应处理
                # Single Query Multiple Response Processing
                if len(batch_queries) == 1 and len(new_translations) > 1:
                    # 检查是否存在无效索引（例如 <|2|>, <|3|> 等）
                    # Check if invalid indexes exist (for example, <|2|>, <|3|>, etc.)
                    has_invalid_index = False
                    for part in new_translations[1:]:  
                        index_match = re.search(r'<\|(\d+)\|>', part)
                        if index_match:
                            index = int(index_match.group(1))
                            if index > 1:  
                                has_invalid_index = True
                                break
                    
                    if has_invalid_index:
                        merged_translation = re.sub(r'<\|\d+\|>', '', response_text).strip()
                        new_translations = [merged_translation]
                        self.logger.warning("Detected split translations for a single query, merged.")
                        merged_single_query = True
                # 清理首空元素
                # Remove leading empty elements
                elif new_translations and not new_translations[0].strip():
                    new_translations = new_translations[1:]
                
                # 严格检查前缀格式  
                # Strictly check prefix format  
                is_valid_format = True  
                if not merged_single_query:
                    lines = response_text.strip().split('\n')  
                    if not lines and len(batch_queries) > 0: # fix: IndexError: list index out of range  
                        self.logger.warning(f"[Attempt {attempt+1}/{max_attempts}] Received empty response for non-empty batch. Retrying...")  
                        is_valid_format = False  
                    else:  
                        # 预期的索引集合，从1开始  
                        # Expected index set, starting from 1  
                        expected_indices = set(range(1, len(batch_queries) + 1))  
                        # 用来跟踪已经找到的索引，检查重复  
                        # Track found indices to check for duplicates  
                        found_indices = set()   
                        non_empty_lines_count = 0  
    
                        # 逐行检查响应格式  
                        # Check response format line by line  
                        for line_idx, line in enumerate(lines):  
                            line = line.strip()  
                            if not line:  
                                continue # 跳过空行 / Skip empty lines  
                            non_empty_lines_count += 1  
    
                            # 严格从行首匹配 <|数字|> 格式  
                            # Strictly match <|number|> format from the beginning of the line  
                            match = re.match(r'^<\|(\d+)\|>(.*)', line)  
                            if match:  
                                try:  
                                    current_index = int(match.group(1))  
                                    if current_index in expected_indices:  
                                        # --- 检查索引是否已经找到过 ---  
                                        # --- Check if the index has already been found ---  
                                        if current_index in found_indices:  
                                            # 如果索引重复，则标记为无效格式并停止检查  
                                            # If index is duplicated, mark as invalid format and stop checking  
                                            self.logger.warning(  
                                                f"[Attempt {attempt+1}/{max_attempts}] Duplicate index {current_index} detected. Line: '{line}'. Retrying..."  
                                            )  
                                            is_valid_format = False  
                                            break # 停止检查当前响应 / Stop checking current response  
                                        else:  
                                            # 如果是第一次遇到这个有效索引，添加到 found_indices  
                                            # If this is the first time encountering this valid index, add to found_indices  
                                            found_indices.add(current_index)  
                                    else:  
                                        # 索引号超出预期范围  
                                        # Index number exceeds expected range  
                                        self.logger.warning(  
                                            f"[Attempt {attempt+1}/{max_attempts}] Invalid index {current_index} found (expected 1-{len(batch_queries)}). Line: '{line}'. Retrying..."  
                                        )  
                                        is_valid_format = False  
                                        break  
                                except ValueError:  
                                    # 基本不会发生  
                                    # This should rarely happen  
                                    self.logger.warning(  
                                        f"[Attempt {attempt+1}/{max_attempts}] Could not parse index from prefix. Line: '{line}'. Retrying..."  
                                    )  
                                    is_valid_format = False  
                                    break  
                            else:
                                # 不再要求每行都有前缀，因为模型可能将一句话换行
                                # No longer requiring each line to have a prefix, because the model may break a sentence into multiple lines.
                                continue 

                    # --- 在检查完所有行后：验证是否找到了足够的索引 ---  
                    # --- After checking all rows: verify if enough indices have been found ---
                    if is_valid_format:  
                        # 检查是否找到了所有预期的索引  
                        # Check if all expected indexes were found
                        if len(found_indices) != len(batch_queries):  
                            self.logger.warning(  
                                f"[Attempt {attempt+1}/{max_attempts}] Found indices count ({len(found_indices)}) does not match expected count ({len(batch_queries)}). Retrying..."  
                            )  
                            is_valid_format = False  
                        else:  
                            # 确保找到的索引集合与预期索引集合一致  
                            # Ensure the found index set matches the expected index set
                            if found_indices != expected_indices:  
                                self.logger.warning(  
                                    f"[Attempt {attempt+1}/{max_attempts}] Found indices set {sorted(list(found_indices))} does not match expected set {sorted(list(expected_indices))}. Retrying..."  
                                )  
                                is_valid_format = False
                    
                # 如果格式检查未通过（包括重复索引、无效索引、缺失索引、无效前缀格式），则重试  
                # If format check fails (including duplicate indices, invalid indices, missing indices, invalid prefix format), retry  
                if not is_valid_format:  
                    #await asyncio.sleep(RETRY_BACKOFF_BASE + attempt * RETRY_BACKOFF_FACTOR) # 格式错误重试前等待并退避  
                    continue # 进入下一次重试 / Proceed to next retry  
                
                # 跳过经常性的模型幻觉字符
                # Skip common hallucination characters in specific models
                SUSPICIOUS_SYMBOLS = ["ହ", "ି", "ഹ"]  
                if any(symbol in response_text for symbol in SUSPICIOUS_SYMBOLS):  
                    self.logger.warn(f'[attempt {attempt+1}/{max_attempts}] Suspicious symbols detected, skipping the current translation attempt.')  
                    continue              
                
             
                # 判断是否有明显的空翻译(只有当原文不为空但译文为空时才报错)  
                # Check for obvious empty translations (only report error when source is not empty but translation is empty)  
                empty_translation_errors = []
                for i, (source, translation) in enumerate(zip(batch_queries, new_translations)):
                    # 当原文不为空但译文为空时，才认为是错误的空翻译
                    # Only consider it an error when source is not empty but translation is empty
                    if source.strip() and not translation:
                        empty_translation_errors.append(i + 1)
                
                if empty_translation_errors:  
                    self.logger.warning(  
                        f"[Attempt {attempt+1}/{max_attempts}] Empty translation detected for non-empty sources at positions: {empty_translation_errors}. Retrying..."  
                    )  
                    # 需要注意，此处也可换成break直接进入分割逻辑。原因是若出现空结果时，不断重试出现正确结果的效率相对较低，可能直到用尽重试错误依然无解。但是为了尽可能确保翻译质量，使用了continue，并相应地下调重试次数以抵消影响。  
                    # Note: This could be changed to break to directly enter the splitting logic. This is because when empty results occur,  
                    # repeatedly retrying for correct results is relatively inefficient and may still fail after all retries.  
                    # However, to ensure translation quality as much as possible, continue is used here, and the number of retries  
                    # is correspondingly reduced to offset the impact.  
                    continue
                
                # 检查特殊串行情况  
                # Check for special merged translation
                is_valid_translation = True  
                for i, (source, translation) in enumerate(zip(batch_queries, new_translations)):  
                    is_source_simple = all(char in string.punctuation for char in source)  
                    is_translation_simple = all(char in string.punctuation for char in translation)  
                    
                    if is_translation_simple and not is_source_simple:  
                        self.logger.warning(  
                            f"[Attempt {attempt+1}/{max_attempts}] Detected potential merged translation. "  
                            f"Source: '{source}', Translation: '{translation}' (index {i+1}). Retrying..."  
                        )  
                        is_valid_translation = False  
                        break  
                        
                if not is_valid_translation:  
                    continue  
                
                # 检查翻译结果数量是否匹配 - 修复 list index out of range 错误
                # Check if the number of translations matches - fix list index out of range error
                if len(new_translations) != len(batch_queries):
                    self.logger.warning(
                        f"[Attempt {attempt+1}/{max_attempts}] Translation count mismatch: "
                        f"got {len(new_translations)} translations for {len(batch_queries)} queries. Retrying..."
                    )
                    continue
                
                # 一切正常，写入 partial_results  
                # Everything is normal, write to partial_results  
                for i in range(len(batch_queries)):  
                    partial_results[i] = new_translations[i]  

                # 成功  
                # Success  
                self.logger.info(  
                    f"Batch of size {len(batch_queries)} translated OK at attempt {attempt+1}/{max_attempts} (split_level={split_level})."  
                )  
                return True, partial_results  

            except Exception as e:  
                self.logger.warning(  
                    f"Batch translate attempt {attempt+1}/{max_attempts} failed with error: {str(e)}"  
                )  
                if attempt < max_attempts - 1:  
                    await asyncio.sleep(1)  
                else:
                    self.logger.warning("Max attempts reached.")
                    # 尝试fallback模型
                    success, fallback_results = await self._try_fallback_model(to_lang, prompt, batch_queries)
                    if success:
                        for i, result in enumerate(fallback_results):
                            partial_results[i] = result
                        self.logger.info("Fallback model succeeded — skipping split logic.")
                        return True, partial_results

        # 循环结束但仍未成功时，再次尝试fallback（如果之前没有因异常触发）
        if not any(partial_results):
            success, fallback_results = await self._try_fallback_model(to_lang, prompt, batch_queries)
            if success:
                for i, result in enumerate(fallback_results):
                    partial_results[i] = result
                self.logger.info("Fallback model succeeded — skipping split logic.")
                return True, partial_results

        # 如果仍然失败 => 尝试拆分。通过减小每次请求的文本量，或者隔离可能导致问题(如产生空行、风控词)的特定 query，来尝试解决问题  
        self.logger.warning("Proceeding to split translation after all retries/fallback failures.")

        if split_level < self._MAX_SPLIT_ATTEMPTS and len(batch_queries) > 1:  
            self.logger.warning(  
                f"Splitting batch of size {len(batch_queries)} at split_level={split_level}"  
            )  
            # 将批量查询分成两半  
            # Split the batch queries into two halves  
            mid = len(batch_queries) // 2  
            left_queries = batch_queries[:mid]  
            right_queries = batch_queries[mid:]  

            left_indices = batch_indices[:mid]  
            right_indices = batch_indices[mid:]  

            # 并发翻译左半部分和右半部分 
            # Concurrently translate the left and right halves
            left_prompt, _ = next(self._assemble_prompts(from_lang, to_lang, left_queries))  
            right_prompt, _ = next(self._assemble_prompts(from_lang, to_lang, right_queries))
            
            # 使用 asyncio.gather 实现并发翻译  
            # Use asyncio.gather for concurrent translation
            self.logger.info(f"Starting split translation: left batch size {len(left_queries)}, right batch size {len(right_queries)}")
            
            try:
                (left_success, left_results), (right_success, right_results) = await asyncio.gather(
                    self._translate_batch(from_lang, to_lang, left_queries, left_indices, left_prompt, split_level+1),
                    self._translate_batch(from_lang, to_lang, right_queries, right_indices, right_prompt, split_level+1),
                    return_exceptions=False
                )
            except Exception as e:
                self.logger.error(f"Error during split translation: {e}")
                # 如果并发失败，回退到串行处理
                self.logger.info("Falling back to sequential processing due to split translation error")
                left_success, left_results = await self._translate_batch(
                    from_lang, to_lang, left_queries, left_indices, left_prompt, split_level+1
                )
                right_success, right_results = await self._translate_batch(
                    from_lang, to_lang, right_queries, right_indices, right_prompt, split_level+1
                )

            # 合并结果  
            # Merge results  
            return (left_success and right_success), (left_results + right_results)  
        else:  
            # 不能再拆分了就返回 区分没有前缀的和分割后依然失败的  
            # If can't split further, return results, distinguishing between those without prefixes and those that still fail after splitting  
            if len(batch_queries) == 1 and not re.match(r'^\s*<\|1\|>', response_text):  
                self.logger.error(  
                    f"Single query translation failed after max retries due to missing prefix. size={len(batch_queries)}"  
                )  
            else:  
                self.logger.error(  
                    f"Translation failed after max retries and splits. Returning original queries. size={len(batch_queries)}"  
                )  
            # 失败的query全部保留原文  
            # Keep all failed queries as original text  
            for i in range(len(batch_queries)):   
                partial_results[i] = batch_queries[i]     
                
            return False, partial_results  

    async def _request_with_retry(self, to_lang: str, prompt: str) -> str:
        """
        结合重试、超时、限流处理的请求入口。
        """
        # 这里演示3层重试: 
        #   1) 如果请求超时 => 重新发起(最多 _TIMEOUT_RETRY_ATTEMPTS 次)
        #   2) 如果返回 429 => 也做重试(最多 _RATELIMIT_RETRY_ATTEMPTS 次)
        #   3) 其他错误 => 重试 _RETRY_ATTEMPTS 次
        # 最终失败则抛异常
        # 也可以将下面逻辑整合到 _translate_batch 里，但保持一次请求一次处理也行。

        timeout_attempt = 0
        ratelimit_attempt = 0
        server_error_attempt = 0

        while True:
            await self._ratelimit_sleep()
            started = time.time()
            req_task = asyncio.create_task(self._request_translation(to_lang, prompt))

            try:
                # 等待请求
                while not req_task.done():
                    await asyncio.sleep(0.1)
                    if time.time() - started > self._TIMEOUT:
                        # 超时 => 取消请求并重试
                        timeout_attempt += 1
                        if timeout_attempt > self._TIMEOUT_RETRY_ATTEMPTS:
                            raise TimeoutError(
                                f"OpenAI request timed out after {self._TIMEOUT_RETRY_ATTEMPTS} attempts."
                            )
                        self.logger.warning(f"Request timed out, retrying... (attempt={timeout_attempt})")
                        req_task.cancel()
                        break
                else:
                    # 如果正常完成了
                    return req_task.result()

            except openai.RateLimitError:
                # 限流 => 重试
                ratelimit_attempt += 1
                if ratelimit_attempt > self._RATELIMIT_RETRY_ATTEMPTS:
                    raise
                self.logger.warning(f"Hit RateLimit, retrying... (attempt={ratelimit_attempt})")
                await asyncio.sleep(2)

            except openai.APIError as e:
                # 服务器错误 => 重试
                server_error_attempt += 1
                if server_error_attempt > self._RETRY_ATTEMPTS:
                    self.logger.error("Server error, giving up after several attempts.")
                    raise
                self.logger.warning(f"Server error: {str(e)}. Retrying... (attempt={server_error_attempt})")
                await asyncio.sleep(1)

            except Exception as e:
                self.logger.error(f"Unexpected error in _request_with_retry: {str(e)}")
                raise

    async def _request_translation(self, to_lang: str, prompt: str) -> str:
        """
        实际调用 openai.ChatCompletion 的请求部分。
        集成术语表功能。
        """
        """
        The actual request part that calls openai.ChatCompletion.
        Incorporate the glossary function.
        """        
        lang_name = self._LANGUAGE_CODE_MAP.get(to_lang, to_lang) if to_lang in self._LANGUAGE_CODE_MAP else to_lang
                
        # 构建 messages / Construct messages
        messages = [  
            {'role': 'system', 'content': self.chat_system_template.format(to_lang=lang_name)},  
        ]  

        # 提取相关术语并添加到系统消息中  / Extract relevant terms and add them to the system message
        has_glossary = False  # 添加标志表示是否有术语表 / Add a flag to indicate whether there is a glossary
        relevant_terms = self.extract_relevant_terms(prompt)  
        if relevant_terms:  
            has_glossary = True  # 设置标志 / Set the flag
            # 构建术语表字符串 / Construct the glossary string
            glossary_text = "\n".join([f"{term}->{translation}" for term, translation in relevant_terms.items()])  
            system_message = self.glossary_system_template.format(glossary_text=glossary_text)  
            messages.append({'role': 'system', 'content': system_message})  
            self.logger.info(f"Loaded {len(relevant_terms)} relevant terms from the glossary.")  
        
        # 如果有上文，添加到系统消息中 / If there is a previous context, add it to the system message        
        if self.prev_context:
            messages.append({'role': 'system', 'content': self.prev_context})            
        
        # 如果需要先给出示例对话
        # Add chat samples if available
        lang_chat_samples = self.get_chat_sample(to_lang)

        # 如果需要先给出示例对话 / Provide an example dialogue first if necessary
        if hasattr(self, 'chat_sample') and lang_chat_samples:
            messages.append({'role': 'user', 'content': lang_chat_samples[0]})
            messages.append({'role': 'assistant', 'content': lang_chat_samples[1]})

        # 最终用户请求 / End-user request 
        messages.append({'role': 'user', 'content': prompt})  

        # 准备输出的 prompt 文本 / Prepare the output prompt text 
        if self.verbose_logging:  
            prompt_text = "\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages) 
                    
            self.print_boxed(prompt_text, border_color="cyan", title="GPT Prompt")      
        else:  
            simplified_msgs = []  
            for i, m in enumerate(messages):  
                if (has_glossary and i == 1) or (i == len(messages) - 1):  
                    simplified_msgs.append(f"{m['role'].upper()}:\n{m['content']}")  
                else:  
                    simplified_msgs.append(f"{m['role'].upper()}:\n[HIDDEN CONTENT]")  
            prompt_text = "\n".join(simplified_msgs)
            # 使用 rich 输出 prompt / Use rich to output the prompt
            self.print_boxed(prompt_text, border_color="cyan", title="GPT Prompt (verbose=False)") 
        

        # 发起请求 / Initiate the request
        response = await self.client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=self._MAX_TOKENS // 2,
            temperature=self.temperature,
            top_p=self.top_p,
            timeout=self._TIMEOUT
        )

        if not response.choices:
            raise ValueError("Empty response from OpenAI API")

        raw_text = response.choices[0].message.content

        # 去除 <think>...</think> 标签及内容。由于某些中转api的模型的思考过程是被强制输出的，并不包含在reasoning_content中，需要额外过滤
        # Remove <think>...</think> tags and their contents. Since the reasoning process of some relay API models is forcibly output and not included in the reasoning_content, additional filtering is required.
        raw_text = re.sub(r'(</think>)?<think>.*?</think>', '', raw_text, flags=re.DOTALL)

        # 删除多余的空行 / Remove extra blank lines
        
        cleaned_text = re.sub(r'\n\s*\n', '\n', raw_text).strip()

        # 删除数字前缀前后的不相关的解释性文字。但不出现数字前缀时，保留限制词防止删得什么都不剩
        # Remove irrelevant explanatory text before and after numerical prefixes. However, when numerical prefixes are not present, retain restrictive words to prevent deleting everything.
        lines = cleaned_text.splitlines()
        min_index_line_index = -1
        max_index_line_index = -1
        has_numeric_prefix = False  # Flag to check if any numeric prefix exists

        for index, line in enumerate(lines):
            match = re.search(r'<\|(\d+)\|>', line)
            if match:
                has_numeric_prefix = True
                current_index = int(match.group(1))
                if current_index == 1:  # 查找最小标号 <|1|> / find <|1|>
                    min_index_line_index = index
                if max_index_line_index == -1 or current_index > int(re.search(r'<\|(\d+)\|>', lines[max_index_line_index]).group(1)):  # 查找最大标号 / find max number
                    max_index_line_index = index
                    
        if has_numeric_prefix:
            modified_lines = []
            if min_index_line_index != -1:
                modified_lines.extend(lines[min_index_line_index:])  # 从最小标号行开始保留到结尾 / Keep from the row with the smallest label to the end

            if max_index_line_index != -1 and modified_lines:  # 确保 modified_lines 不为空，且找到了最大标号 / Ensure that modified_lines is not empty and that the maximum label has been found
                modified_lines = modified_lines[:max_index_line_index - min_index_line_index + 1]  # 只保留到最大标号行 (相对于 modified_lines 的索引) / Retain only up to the row with the maximum label (relative to the index of modified_lines)

            cleaned_text = "\n".join(modified_lines)      
        
        # 记录 token 消耗 / Record token consumption
        if not hasattr(response, 'usage') or not hasattr(response.usage, 'total_tokens'):
            self.logger.warning("Response does not contain usage information") #第三方逆向中转api不返回token数 / The third-party reverse proxy API does not return token counts
            self.token_count_last = 0
            
        # 记录 token 消耗   (rich模式) / Record token consumption (rich mode)
        # if not hasattr(response, 'usage') or not hasattr(response.usage, 'total_tokens'):  
            # warning_text = "WARNING: [OpenAITranslator] Response does not contain usage information"  
            # self.print_boxed(warning_text, border_color="yellow")  
            # self.token_count_last = 0              
            
        else:
            self.token_count += response.usage.total_tokens
            self.token_count_last = response.usage.total_tokens
        
        response_text = cleaned_text
        self.print_boxed(response_text, border_color="green", title="GPT Response")          
        return cleaned_text

    def _fix_prefix_spacing(self, text_to_fix):
        """修复前缀和翻译内容之间的空格问题"""
        lines = text_to_fix.strip().split('\n')
        fixed_lines = []
        
        for line in lines:
            # 匹配 <|数字|> 前缀格式，去除前缀后的多余空格
            # Match <|number|> prefix format and remove extra spaces after prefix
            match = re.match(r'^(<\|\d+\|>)\s+(.*)$', line.strip())
            if match:
                prefix = match.group(1)
                content = match.group(2)
                # 重新组合：前缀 + 内容
                # Recombine: prefix + content (no space in between)
                fixed_line = f"{prefix}{content}"
                fixed_lines.append(fixed_line)
            else:
                fixed_lines.append(line)
        
        return '\n'.join(fixed_lines)

    # ==============修改日志输出方法 (Modify Log Output Method)==============
    def print_boxed(self, text, border_color="blue", title="OpenAITranslator Output"):  
        """将文本框起来并输出到终端"""
        """Box the text and output it to the terminal"""    
        
        # 应用修复
        # Apply the fix
        fixed_text = self._fix_prefix_spacing(text)
        
        # 输出到控制台（带颜色和边框）
        panel = Panel(fixed_text, title=title, border_style=border_color, expand=False)  
        self.console.print(panel)
        
        # 同时输出到日志文件（纯文本格式）
        
        if hasattr(manga_translator, '_log_console') and manga_translator._log_console:
            # 直接输出纯文本，不使用边框
            manga_translator._log_console.print(f"=== {title} ===")
            manga_translator._log_console.print(fixed_text)
            manga_translator._log_console.print("=" * (len(title) + 8))

    # ==============以下是术语表相关函数 (Below are glossary-related functions)==============
    
    def load_glossary(self, path):
        """加载术语表文件 / Load the glossary file"""
        if not os.path.exists(path):
            # 只在第一次检查时显示警告
            if not OpenAITranslator._glossary_warning_shown:
                self.logger.warning(f"The OpenAI glossary file does not exist: {path}")
                OpenAITranslator._glossary_warning_shown = True
            return {}
                
        # 检测文件类型并解析 / Detect the file type and parse it
        dict_type = self.detect_type(path)
        if dict_type == "galtransl":
            return self.load_galtransl_dic(path)
        elif dict_type == "sakura":
            return self.load_sakura_dict(path)
        elif dict_type == "mit":
            return self.load_mit_dict(path)              
        else:
            self.logger.warning(f"Unknown OpenAI glossary format: {path}")
            return {}

    def detect_type(self, dic_path):  
        """  
        检测字典类型（OpenAI专用） / Detect dictionary type (specific to OpenAI).
        """  
        with open(dic_path, encoding="utf8") as f:  
            dic_lines = f.readlines()  
        self.logger.debug(f"Detecting OpenAI dictionary type: {dic_path}")  
        if len(dic_lines) == 0:  
            return "unknown"  

        # 先判断是否为Sakura字典 / First, determine if it is a Sakura dictionary
        is_sakura = True  
        sakura_line_count = 0  
        for line in dic_lines:  
            line = line.strip()  
            if not line or line.startswith("\\\\") or line.startswith("//"):  
                continue  
                
            if "->" in line:  
                sakura_line_count += 1  
            else:  
                is_sakura = False  
                break  
        
        if is_sakura and sakura_line_count > 0:  
            return "sakura"  

        # 判断是否为Galtransl字典 / Determine if it is a Galtransl dictionary
        is_galtransl = True  
        galtransl_line_count = 0  
        for line in dic_lines:  
            line = line.strip()  
            if not line or line.startswith("\\\\") or line.startswith("//"):  
                continue  

            if "\t" in line or "    " in line:  
                galtransl_line_count += 1  
            else:  
                is_galtransl = False  
                break  
        
        if is_galtransl and galtransl_line_count > 0:  
            return "galtransl"  

        # 判断是否为MIT字典（最宽松的格式） / Determine if it is an MIT dictionary (the most lenient format)
        is_mit = True  
        mit_line_count = 0  
        for line in dic_lines:  
            line = line.strip()  
            if not line or line.startswith("#") or line.startswith("//"):  
                continue  
                
            # 排除Sakura格式特征 / Exclude Sakura format characteristics
            if "->" in line:  
                is_mit = False  
                break  
                
            # MIT格式需要能分割出源和目标两部分 / The MIT format needs to be able to split into source and target parts
            parts = line.split("\t", 1)  
            if len(parts) == 1:  # 如果没有制表符，尝试用空格分割 / If there are no tab characters, attempt to split using spaces
                parts = line.split(None, 1)  # None表示任何空白字符 / None represents any whitespace character
            
            if len(parts) >= 2:  # 确保有源和目标两部分 / Ensure there are both source and target parts
                mit_line_count += 1  
            else:  
                is_mit = False  
                break  
        
        if is_mit and mit_line_count > 0:  
            return "mit"  

        return "unknown"  

    def load_mit_dict(self, dic_path):
        """载入MIT格式的字典，返回结构化数据，并验证正则表达式"""
        """Load the MIT format dictionary, return structured data, and validate the regular expression."""
        with open(dic_path, encoding="utf8") as f:
            dic_lines = f.readlines()
            
        if len(dic_lines) == 0:
            return {}
            
        dic_path = os.path.abspath(dic_path)
        dic_name = os.path.basename(dic_path)
        dict_count = 0
        regex_errors = 0
        
        glossary_entries = {}
        
        for line_number, line in enumerate(dic_lines, start=1):
            line = line.strip()
            # 跳过空行和注释行 / Skip empty lines and comment lines
            if not line or line.startswith("#") or line.startswith("//"):
                continue
                
            # 处理注释 / Process comments
            comment = ""
            if '#' in line:
                parts = line.split('#', 1)
                line = parts[0].strip()
                comment = "#" + parts[1]
            elif '//' in line:
                parts = line.split('//', 1)
                line = parts[0].strip()
                comment = "//" + parts[1]
            
            # 先尝试用制表符分割源词和目标词
            # First, try to split the source word and target word using a tab character
            parts = line.split("\t", 1)
            if len(parts) == 1:  # 如果没有制表符，尝试用空格分割 / If there is no tab character, try to split using spaces
                parts = line.split(None, 1)  # None表示任何空白字符 / None represents any whitespace character
            
            if len(parts) < 2:
                # 只有一个单词，跳过或记录警告 / If there is only one word, skip it or log a warning
                self.logger.debug(f"Skipping lines with a single word: {line}")
                continue
            else:
                # 源词和目标词 / Source word and target word
                src = parts[0].strip().replace('_', ' ')
                dst = parts[1].strip().replace('_', ' ')  
            
            # 验证正则表达式 / Validate the regular expression
            try:
                re.compile(src)
                # 正则表达式有效，将术语添加到字典中 / The regular expression is valid; add the term to the dictionary
                if comment:
                    entry = f"{dst} {comment}"
                else:
                    entry = dst
                
                glossary_entries[src] = entry
                dict_count += 1
            except re.error as e:
                # 正则表达式无效，记录错误 / The regular expression is invalid; log the error        
                regex_errors += 1
                error_message = str(e)
                self.logger.warning(f"Regular expression error on line {line_number}: '{src}' - {error_message}")
                
                # 提供修复建议 / Provide suggestions for fixes
                suggested_fix = src
                # 转义所有特殊字符 / Escape all special characters
                special_chars = {
                    '[': '\\[', ']': '\\]',
                    '(': '\\(', ')': '\\)',
                    '{': '\\{', '}': '\\}',
                    '.': '\\.', '*': '\\*',
                    '+': '\\+', '?': '\\?',
                    '|': '\\|', '^': '\\^',
                    '$': '\\$', '\\': '\\\\',
                    '/': '\\/'
                }
                
                for char, escaped in special_chars.items():
                    # 已经被转义的不处理 / Do not process characters that are already escaped
                    suggested_fix = re.sub(f'(?<!\\\\){re.escape(char)}', escaped, suggested_fix)
                
                # 特殊处理特定错误型 / Special handling for specific error types
                if "unterminated character set" in error_message:
                    # 如果是未闭合的字符集，查找最后一个'['并添加对应的']'
                    # If it is an unclosed character set, find the last '[' and add the corresponding ']'
                    last_open = suggested_fix.rfind('\\[')
                    if last_open != -1 and '\\]' not in suggested_fix[last_open:]:
                        suggested_fix += '\\]'
                
                elif "unbalanced parenthesis" in error_message:
                    # 如果是括号不平衡，检查并添加缺失的')'
                    # If the parentheses are unbalanced, check and add the missing ')'
                    open_count = suggested_fix.count('\\(')
                    close_count = suggested_fix.count('\\)')
                    if open_count > close_count:
                        suggested_fix += '\\)' * (open_count - close_count)
                    
                self.logger.info(f"Possible fix suggestions: '{suggested_fix}'")
        
        self.logger.info(f"Loading MIT format dictionary: {dic_name} containing {dict_count} entries, found {regex_errors} regular expression errors")
        return glossary_entries

    def load_galtransl_dic(self, dic_path):  
        """载入Galtransl格式的字典 / Loading a Galtransl format dictionary"""  
        glossary_entries = {}  
        
        try:  
            with open(dic_path, encoding="utf8") as f:  
                dic_lines = f.readlines()  
            
            if len(dic_lines) == 0:  
                return {}  
                
            dic_path = os.path.abspath(dic_path)  
            dic_name = os.path.basename(dic_path)  
            normalDic_count = 0  
            
            for line in dic_lines:  
                if line.startswith("\\\\") or line.startswith("//") or line.strip() == "":  
                    continue  
                
                # 尝试用制表符分割 / Attempting to split using tabs
                parts = line.split("\t")  
                # 如果分割结果不符合预期，尝试用空格分割 / If the split result is not as expected, try splitting using spaces    
                    
                if len(parts) != 2:  
                    parts = line.split("    ", 1)  # 四个空格 / Four spaces  
                
                if len(parts) == 2:  
                    src, dst = parts[0].strip(), parts[1].strip()  
                    glossary_entries[src] = dst  
                    normalDic_count += 1  
                else:  
                    self.logger.debug(f"Skipping lines that do not conform to the format.: {line.strip()}")  
            
            self.logger.info(f"Loading Galtransl format dictionary: {dic_name} containing {normalDic_count} entries")  
            return glossary_entries  
            
        except Exception as e:  
            self.logger.error(f"Error loading Galtransl dictionary: {e}")  
            return {}  

    def load_sakura_dict(self, dic_path):  
        """载入Sakura格式的字典 / Loading a Sakura format dictionary"""
        glossary_entries = {}  
        
        try:  
            with open(dic_path, encoding="utf8") as f:  
                dic_lines = f.readlines()  
            
            if len(dic_lines) == 0:  
                return {}  
                
            dic_path = os.path.abspath(dic_path)  
            dic_name = os.path.basename(dic_path)  
            dict_count = 0  
            
            for line in dic_lines:  
                line = line.strip()  
                if line.startswith("\\\\") or line.startswith("//") or line == "":  
                    continue  
                
                # Sakura格式使用 -> 分隔源词和目标词 /  
                # Sakura format uses -> to separate source words and target words
                if "->" in line:  
                    parts = line.split("->", 1)  
                    if len(parts) == 2:  
                        src, dst = parts[0].strip(), parts[1].strip()  
                        glossary_entries[src] = dst  
                        dict_count += 1  
                    else:  
                        self.logger.debug(f"Skipping lines that do not conform to the format: {line}")  
                else:  
                    self.logger.debug(f"Skipping lines that do not conform to the format: {line}")  
            
            self.logger.info(f"Loading Sakura format dictionary: {dic_name} containing {dict_count} entries")  
            return glossary_entries  
            
        except Exception as e:  
            self.logger.error(f"Error loading Sakura dictionary: {e}")  
            return {}       
            
    def extract_relevant_terms(self, text):  
        """自动提取和query相关的术语表条目，而不是一次性将术语表载入全部，以防止token浪费和系统提示词权重下降导致的指导效果减弱"""
        """Automatically extract glossary entries related to the query, 
           rather than loading the entire glossary at once, 
           to prevent token wastage and reduced guidance effectiveness due to a decrease in system prompt weight."""
        relevant_terms = {}  
        
        # 1. 编辑距离计算函数 / Edit distance calculation function 
        def levenshtein_distance(s1, s2):  
            if len(s1) < len(s2):  
                return levenshtein_distance(s2, s1)  
            if len(s2) == 0:  
                return len(s1)  
            
            previous_row = range(len(s2) + 1)  
            for i, c1 in enumerate(s1):  
                current_row = [i + 1]  
                for j, c2 in enumerate(s2):  
                    insertions = previous_row[j + 1] + 1  
                    deletions = current_row[j] + 1  
                    substitutions = previous_row[j] + (c1 != c2)  
                    current_row.append(min(insertions, deletions, substitutions))  
                previous_row = current_row  
            
            return previous_row[-1]  
        
        # 日语专用编辑距离计算 / Edit distance calculation specifically for Japanese  
        def japanese_levenshtein_distance(s1, s2):  
            # 先将两个字符串规范化为同一种写法 / First, normalize both strings to the same writing system. 
            s1 = normalize_japanese(s1)  
            s2 = normalize_japanese(s2)  
            # 计算规范化后的编辑距离 / Calculate the edit distance after normalization 
            return levenshtein_distance(s1, s2)  
        
        # 2. 日语文本规范化（将片假名转为平假名） / Japanese text normalization (convert katakana to hiragana)  
        def normalize_japanese(text):  
            result = ""  
            for char in text:  
                # 小写片假名映射到标准片假名 (Map lowercase katakana to standard katakana) 
                # 可能导致较轻的过拟合，但是目前的OCR检测日语会大小写不分的情况下这不可或缺，有更强大的OCR时可移除
                # It may result in a slight overfitting, but it is indispensable under the current OCR conditions where Japanese detection is case-insensitive.
                small_to_normal = {  
                    'ァ': 'ア', 'ィ': 'イ', 'ゥ': 'ウ', 'ェ': 'エ', 'ォ': 'オ',  
                    'ッ': 'ツ', 'ャ': 'ヤ', 'ュ': 'ユ', 'ョ': 'ヨ',  
                    'ぁ': 'あ', 'ぃ': 'い', 'ぅ': 'う', 'ぇ': 'え', 'ぉ': 'お',  
                    'っ': 'つ', 'ゃ': 'や', 'ゅ': 'ゆ', 'ょ': 'よ'  
                }  
                
                # 先处理小写字符 (First, process the lowercase characters) 
                if char in small_to_normal:  
                    char = small_to_normal[char]  
                    
                # 检查是否是片假名范围 (0x30A0-0x30FF)  
                # Check if it's within the katakana range (0x30A0-0x30FF)
                if 0x30A0 <= ord(char) <= 0x30FF:  
                    # 转换片假名到平假名 (减去0x60)  
                    # Convert katakana to hiragana (subtract 0x60)
                    hiragana_char = chr(ord(char) - 0x60)  
                    result += hiragana_char  
                else:  
                    result += char  
            return result  
        
        # 3. 增强的词规范化处理 / Enhanced word normalization processing          
        def normalize_term(term):
            # 基础处理 (Basic processing)
            term = re.sub(r'[^\w\s]', '', term)  # 移除标点符号 (Remove punctuation)
            term = term.lower()                   # 转换为小写 (Convert to lowercase)
            # 日语处理 (Japanese processing)
            term = normalize_japanese(term)       # 片假名转平假名 (Convert katakana to hiragana)
            return term
        
        # 4. 部分匹配函数 / Partial match function
        def partial_match(text, term):  
            normalized_text = normalize_term(text)  
            normalized_term = normalize_term(term)  
            return normalized_term in normalized_text  

        # 5. 日语特化的相似度判断 (Japanese-specific similarity judgment)
        def is_japanese_similar(text, term, threshold=2):
            # 规范化后计算编辑距离 (Calculate edit distance after normalization)
            normalized_text = normalize_term(text)
            normalized_term = normalize_term(term)

            # 如果术语很短，降低阈值 (Reduce the threshold if the term is short)
            if len(normalized_term) <= 2:
                threshold = 0
            elif len(normalized_term) <= 4:  
                threshold = 1

            # # 滑动窗口匹配（针对较长文本和短术语）- 可能过拟合，需要进一步调整 (Sliding window matching (for longer texts and short terms) - May overfit, needs further adjustment)
            # if len(normalized_text) > len(normalized_term):
            #     min_distance = float('inf')
            #     # 创建与术语等长的窗口，在文本中滑动 (Create a window of the same length as the term and slide it through the text)
            #     for i in range(len(normalized_text) - len(normalized_term) + 1):
            #         window = normalized_text[i:i+len(normalized_term)]
            #         distance = japanese_levenshtein_distance(window, normalized_term)
            #         min_distance = min(min_distance, distance)
            #     return min_distance <= threshold
            # else:
            #     # 直接计算编辑距离 (Calculate the edit distance directly)
            #     distance = japanese_levenshtein_distance(normalized_text, normalized_term)
            #     return distance <= threshold

            # 直接计算编辑距离 (Calculate the edit distance directly)
            distance = japanese_levenshtein_distance(normalized_text, normalized_term)
            return distance <= threshold

        # 6. 普通文本的相似度判断 / Similarity judgment for general text  
        def is_general_similar(text, term, threshold=2):  
            # 规范化后计算编辑距离 / Calculate edit distance after normalization  
            normalized_text = normalize_term(text)  
            normalized_term = normalize_term(term)  
            
            # 根据术语长度动态调整阈值 / Dynamically adjust threshold based on term length  
            threshold = len(normalized_term) // 8  

            # 限制阈值范围 / Limit the threshold range  
            threshold = max(0, min(threshold, 3))      
            
            # 对于较长文本，使用滑动窗口匹配 / For longer texts, use sliding window matching  
            if len(normalized_text) > len(normalized_term) * 5:  
                min_distance = float('inf')  
                # 创建比术语略长的窗口，在文本中滑动 / Create a window slightly larger than the term and slide it through the text  
                if len(normalized_term) <= 8:  
                    window_size = len(normalized_term)   
                elif len(normalized_term) <= 16:  
                    window_size = len(normalized_term) + 1  
                else:  
                    window_size = len(normalized_term) + 2    
                for i in range(max(0, len(normalized_text) - window_size + 1)):  
                    window = normalized_text[i:i+window_size]  
                    distance = levenshtein_distance(window, normalized_term)  
                    min_distance = min(min_distance, distance)  
                return min_distance <= threshold  
            else:  
                # 直接计算编辑距离 / Calculate the edit distance directly  
                distance = levenshtein_distance(normalized_text, normalized_term)  
                return distance <= threshold  
        
        # 主匹配逻辑 (Main matching logic)
        for term, translation in self.glossary_entries.items():
            # 1. 精确匹配：同时检查原词和去除空格的变体是否出现在文本中 (Exact Match: Check whether both the original word and its variant with spaces removed appear in the text)
            if term in text or term.replace(" ", "") in text:
                relevant_terms[term] = translation
                continue

            # 2. 日语特化的相似度匹配 (Japanese-specific similarity matching)
            if any(c for c in term if 0x3040 <= ord(c) <= 0x30FF):  # 检查是否包含日语字符 (Check if it contains Japanese characters)
                if is_japanese_similar(text, term):
                    relevant_terms[term] = translation
                    continue

            # 3. 普通编辑距离匹配（非日语文本） / Ordinary edit distance matching (non-Japanese text)  
            elif is_general_similar(text, term):  
                relevant_terms[term] = translation  
                continue  

            # 4. 部分匹配 (Partial match)
            if partial_match(text, term):
                relevant_terms[term] = translation
                continue

            # 5. 正则表达式匹配 (Regular expression matching)
            pattern = re.compile(term, re.IGNORECASE)
            if pattern.search(text):
                relevant_terms[term] = translation

        return relevant_terms 
