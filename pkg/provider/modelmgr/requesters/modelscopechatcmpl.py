from __future__ import annotations

import asyncio
import json
import typing

import openai
import openai.types.chat.chat_completion as chat_completion
import openai.types.chat.chat_completion_message_tool_call as chat_completion_message_tool_call
import httpx

from .. import entities, errors, requester
from ....core import entities as core_entities
from ... import entities as llm_entities
from ...tools import entities as tools_entities


class ModelScopeChatCompletions(requester.ProviderAPIRequester):
    """ModelScope ChatCompletion API 请求器"""

    client: openai.AsyncClient

    default_config: dict[str, typing.Any] = {
        'base_url': 'https://api-inference.modelscope.cn/v1',
        'timeout': 120,
    }

    async def initialize(self):
        self.client = openai.AsyncClient(
            api_key='',
            base_url=self.requester_cfg['base_url'],
            timeout=self.requester_cfg['timeout'],
            http_client=httpx.AsyncClient(trust_env=True, timeout=self.requester_cfg['timeout']),
        )

    async def _req(
        self,
        query: core_entities.Query,
        args: dict,
        extra_body: dict = {},
        remove_think: bool = False,
    ) -> list[dict[str, typing.Any]]:
        args['stream'] = True

        chunk = None

        pending_content = ''

        tool_calls = []

        resp_gen: openai.AsyncStream = await self.client.chat.completions.create(**args, extra_body=extra_body)

        chunk_idx = 0
        thinking_started = False
        thinking_ended = False
        tool_id = ''
        tool_name = ''
        message_delta = {}
        async for chunk in resp_gen:
            if not chunk or not chunk.id or not chunk.choices or not chunk.choices[0] or not chunk.choices[0].delta:
                continue

            delta = chunk.choices[0].delta.model_dump() if hasattr(chunk.choices[0], 'delta') else {}
            reasoning_content = delta.get('reasoning_content')
            # 处理 reasoning_content
            if reasoning_content:
                # accumulated_reasoning += reasoning_content
                # 如果设置了 remove_think，跳过 reasoning_content
                if remove_think:
                    chunk_idx += 1
                    continue

                # 第一次出现 reasoning_content，添加 <think> 开始标签
                if not thinking_started:
                    thinking_started = True
                    pending_content += '<think>\n' + reasoning_content
                else:
                    # 继续输出 reasoning_content
                    pending_content += reasoning_content
            elif thinking_started and not thinking_ended and delta.get('content'):
                # reasoning_content 结束，normal content 开始，添加 </think> 结束标签
                thinking_ended = True
                pending_content += '\n</think>\n' + delta.get('content')

            if delta.get('content') is not None:
                pending_content += delta.get('content')

            if delta.get('tool_calls') is not None:
                for tool_call in delta.get('tool_calls'):
                    if tool_call['id'] != '':
                        tool_id = tool_call['id']
                    if tool_call['function']['name'] is not None:
                        tool_name = tool_call['function']['name']
                    if tool_call['function']['arguments'] is None:
                        continue
                    tool_call['id'] = tool_id
                    tool_call['name'] = tool_name
                    for tc in tool_calls:
                        if tc['index'] == tool_call['index']:
                            tc['function']['arguments'] += tool_call['function']['arguments']
                            break
                    else:
                        tool_calls.append(tool_call)

            if chunk.choices[0].finish_reason is not None:
                break
        message_delta['content'] = pending_content
        message_delta['role'] = 'assistant'

        message_delta['tool_calls'] = tool_calls if tool_calls else None
        # print(message_delta)
        return [message_delta]

    async def _make_msg(
        self,
        chat_completion: list[dict[str, typing.Any]],
    ) -> llm_entities.Message:
        chatcmpl_message = chat_completion[0]

        # 确保 role 字段存在且不为 None
        if 'role' not in chatcmpl_message or chatcmpl_message['role'] is None:
            chatcmpl_message['role'] = 'assistant'
        # print(chatcmpl_message)
        message = llm_entities.Message(**chatcmpl_message)

        return message

    async def _closure(
        self,
        query: core_entities.Query,
        req_messages: list[dict],
        use_model: requester.RuntimeLLMModel,
        use_funcs: list[tools_entities.LLMFunction] = None,
        extra_args: dict[str, typing.Any] = {},
        remove_think:bool = False,
    ) -> llm_entities.Message:
        self.client.api_key = use_model.token_mgr.get_token()

        args = {}
        args['model'] = use_model.model_entity.name

        if use_funcs:
            tools = await self.ap.tool_mgr.generate_tools_for_openai(use_funcs)

            if tools:
                args['tools'] = tools

        # 设置此次请求中的messages
        messages = req_messages.copy()

        # 检查vision
        for msg in messages:
            if 'content' in msg and isinstance(msg['content'], list):
                for me in msg['content']:
                    if me['type'] == 'image_base64':
                        me['image_url'] = {'url': me['image_base64']}
                        me['type'] = 'image_url'
                        del me['image_base64']

        args['messages'] = messages

        # 发送请求
        resp = await self._req(query, args, extra_body=extra_args, remove_think=remove_think)

        # 处理请求结果
        message = await self._make_msg(resp)

        return message

    async def _req_stream(
        self,
        args: dict,
        extra_body: dict = {},
    ) -> chat_completion.ChatCompletion:
        async for chunk in await self.client.chat.completions.create(**args, extra_body=extra_body):
            yield chunk


    async def _closure_stream(
        self,
        query: core_entities.Query,
        req_messages: list[dict],
        use_model: requester.RuntimeLLMModel,
        use_funcs: list[tools_entities.LLMFunction] = None,
        extra_args: dict[str, typing.Any] = {},
        remove_think: bool = False,
    ) -> llm_entities.Message | typing.AsyncGenerator[llm_entities.MessageChunk, None]:
        self.client.api_key = use_model.token_mgr.get_token()

        args = {}
        args['model'] = use_model.model_entity.name

        if use_funcs:
            tools = await self.ap.tool_mgr.generate_tools_for_openai(use_funcs)

            if tools:
                args['tools'] = tools

        # 设置此次请求中的messages
        messages = req_messages.copy()

        # 检查vision
        for msg in messages:
            if 'content' in msg and isinstance(msg['content'], list):
                for me in msg['content']:
                    if me['type'] == 'image_base64':
                        me['image_url'] = {'url': me['image_base64']}
                        me['type'] = 'image_url'
                        del me['image_base64']

        args['messages'] = messages
        args['stream'] = True


        # 流式处理状态
        tool_calls_map: dict[str, llm_entities.ToolCall] = {}
        chunk_idx = 0
        thinking_started = False
        thinking_ended = False
        role = 'assistant'  # 默认角色
        # accumulated_reasoning = ''  # 仅用于判断何时结束思维链

        async for chunk in self._req_stream(args, extra_body=extra_args):
            # 解析 chunk 数据
            if hasattr(chunk, 'choices') and chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta.model_dump() if hasattr(choice, 'delta') else {}
                finish_reason = getattr(choice, 'finish_reason', None)
            else:
                delta = {}
                finish_reason = None

            # 从第一个 chunk 获取 role，后续使用这个 role
            if 'role' in delta and delta['role']:
                role = delta['role']

            # 获取增量内容
            delta_content = delta.get('content', '')
            reasoning_content = delta.get('reasoning_content', '')

            # 处理 reasoning_content
            if reasoning_content:
                # accumulated_reasoning += reasoning_content
                # 如果设置了 remove_think，跳过 reasoning_content
                if remove_think:
                    chunk_idx += 1
                    continue

                # 第一次出现 reasoning_content，添加 <think> 开始标签
                if not thinking_started:
                    thinking_started = True
                    delta_content = '<think>\n' + reasoning_content
                else:
                    # 继续输出 reasoning_content
                    delta_content = reasoning_content
            elif thinking_started and not thinking_ended and delta_content:
                # reasoning_content 结束，normal content 开始，添加 </think> 结束标签
                thinking_ended = True
                delta_content = '\n</think>\n' + delta_content

            # 处理 content 中已有的 <think> 标签（如果需要移除）
            # if delta_content and remove_think and '<think>' in delta_content:
            #     import re
            #
            #     # 移除 <think> 标签及其内容
            #     delta_content = re.sub(r'<think>.*?</think>', '', delta_content, flags=re.DOTALL)

            # 处理工具调用增量
            if delta.get('tool_calls'):
                for tool_call in delta['tool_calls']:
                    if tool_call['id'] != '':
                        tool_id = tool_call['id']
                    if tool_call['function']['name'] is not None:
                        tool_name = tool_call['function']['name']

                    if tool_call['type'] is None:
                        tool_call['type'] = 'function'
                    tool_call['id'] = tool_id
                    tool_call['function']['name'] = tool_name
                    tool_call['function']['arguments'] = "" if tool_call['function']['arguments'] is None else tool_call['function']['arguments']


            # 跳过空的第一个 chunk（只有 role 没有内容）
            if chunk_idx == 0 and not delta_content and not reasoning_content and not delta.get('tool_calls'):
                chunk_idx += 1
                continue

            # 构建 MessageChunk - 只包含增量内容
            chunk_data = {
                'role': role,
                'content': delta_content if delta_content else None,
                'tool_calls': delta.get('tool_calls'),
                'is_final': bool(finish_reason),
            }

            # 移除 None 值
            chunk_data = {k: v for k, v in chunk_data.items() if v is not None}

            yield llm_entities.MessageChunk(**chunk_data)
            chunk_idx += 1
            # return

    async def invoke_llm(
        self,
        query: core_entities.Query,
        model: entities.LLMModelInfo,
        messages: typing.List[llm_entities.Message],
        funcs: typing.List[tools_entities.LLMFunction] = None,
        extra_args: dict[str, typing.Any] = {},
        remove_think: bool = False,
    ) -> llm_entities.Message:
        req_messages = []  # req_messages 仅用于类内，外部同步由 query.messages 进行
        for m in messages:
            msg_dict = m.dict(exclude_none=True)
            content = msg_dict.get('content')
            if isinstance(content, list):
                # 检查 content 列表中是否每个部分都是文本
                if all(isinstance(part, dict) and part.get('type') == 'text' for part in content):
                    # 将所有文本部分合并为一个字符串
                    msg_dict['content'] = '\n'.join(part['text'] for part in content)
            req_messages.append(msg_dict)

        try:
            return await self._closure(
                query=query, req_messages=req_messages, use_model=model, use_funcs=funcs, extra_args=extra_args, remove_think=remove_think
            )
        except asyncio.TimeoutError:
            raise errors.RequesterError('请求超时')
        except openai.BadRequestError as e:
            if 'context_length_exceeded' in e.message:
                raise errors.RequesterError(f'上文过长，请重置会话: {e.message}')
            else:
                raise errors.RequesterError(f'请求参数错误: {e.message}')
        except openai.AuthenticationError as e:
            raise errors.RequesterError(f'无效的 api-key: {e.message}')
        except openai.NotFoundError as e:
            raise errors.RequesterError(f'请求路径错误: {e.message}')
        except openai.RateLimitError as e:
            raise errors.RequesterError(f'请求过于频繁或余额不足: {e.message}')
        except openai.APIError as e:
            raise errors.RequesterError(f'请求错误: {e.message}')

    async def invoke_llm_stream(
        self,
        query: core_entities.Query,
        model: requester.RuntimeLLMModel,
        messages: typing.List[llm_entities.Message],
        funcs: typing.List[tools_entities.LLMFunction] = None,
        extra_args: dict[str, typing.Any] = {},
        remove_think: bool = False,
    ) -> llm_entities.MessageChunk:
        req_messages = []  # req_messages 仅用于类内，外部同步由 query.messages 进行
        for m in messages:
            msg_dict = m.dict(exclude_none=True)
            content = msg_dict.get('content')
            if isinstance(content, list):
                # 检查 content 列表中是否每个部分都是文本
                if all(isinstance(part, dict) and part.get('type') == 'text' for part in content):
                    # 将所有文本部分合并为一个字符串
                    msg_dict['content'] = '\n'.join(part['text'] for part in content)
            req_messages.append(msg_dict)

        try:
            async for item in self._closure_stream(
                query=query,
                req_messages=req_messages,
                use_model=model,
                use_funcs=funcs,
                extra_args=extra_args,
                remove_think=remove_think,
            ):
                yield item

        except asyncio.TimeoutError:
            raise errors.RequesterError('请求超时')
        except openai.BadRequestError as e:
            if 'context_length_exceeded' in e.message:
                raise errors.RequesterError(f'上文过长，请重置会话: {e.message}')
            else:
                raise errors.RequesterError(f'请求参数错误: {e.message}')
        except openai.AuthenticationError as e:
            raise errors.RequesterError(f'无效的 api-key: {e.message}')
        except openai.NotFoundError as e:
            raise errors.RequesterError(f'请求路径错误: {e.message}')
        except openai.RateLimitError as e:
            raise errors.RequesterError(f'请求过于频繁或余额不足: {e.message}')
        except openai.APIError as e:
            raise errors.RequesterError(f'请求错误: {e.message}')
