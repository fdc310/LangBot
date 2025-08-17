from __future__ import annotations

import json
import copy
import typing
from .. import runner
from ...core import entities as core_entities
from .. import entities as llm_entities


rag_combined_prompt_template = """
The following are relevant context entries retrieved from the knowledge base. 
Please use them to answer the user's message. 
Respond in the same language as the user's input.

<context>
{rag_context}
</context>

<user_message>
{user_message}
</user_message>
"""


@runner.runner_class('local-agent')
class LocalAgentRunner(runner.RequestRunner):
    """本地Agent请求运行器"""

    class ToolCallTracker:
        """工具调用追踪器"""

        def __init__(self):
            self.active_calls: dict[str, dict] = {}
            self.completed_calls: list[llm_entities.ToolCall] = []

    async def run(
        self, query: core_entities.Query
    ) -> typing.AsyncGenerator[llm_entities.Message | llm_entities.MessageChunk, None]:
        """运行请求"""
        pending_tool_calls = []

        kb_uuid = query.pipeline_config['ai']['local-agent']['knowledge-base']

        if kb_uuid == '__none__':
            kb_uuid = None

        user_message = copy.deepcopy(query.user_message)

        user_message_text = ''

        if isinstance(user_message.content, str):
            user_message_text = user_message.content
        elif isinstance(user_message.content, list):
            for ce in user_message.content:
                if ce.type == 'text':
                    user_message_text += ce.text
                    break

        if kb_uuid and user_message_text:
            # only support text for now
            kb = await self.ap.rag_mgr.get_knowledge_base_by_uuid(kb_uuid)

            if not kb:
                self.ap.logger.warning(f'Knowledge base {kb_uuid} not found')
                raise ValueError(f'Knowledge base {kb_uuid} not found')

            result = await kb.retrieve(user_message_text)

            final_user_message_text = ''

            if result:
                rag_context = '\n\n'.join(
                    f'[{i + 1}] {entry.metadata.get("text", "")}' for i, entry in enumerate(result)
                )
                final_user_message_text = rag_combined_prompt_template.format(
                    rag_context=rag_context, user_message=user_message_text
                )

            else:
                final_user_message_text = user_message_text

            self.ap.logger.debug(f'Final user message text: {final_user_message_text}')

            for ce in user_message.content:
                if ce.type == 'text':
                    ce.text = final_user_message_text
                    break

        req_messages = query.prompt.messages.copy() + query.messages.copy() + [user_message]

        try:
            is_stream = await query.adapter.is_stream_output_supported()
        except AttributeError:
            is_stream = False

        remove_think = self.pipeline_config['output'].get('misc', '').get('remove-think')

        if not is_stream:
            # 非流式输出，直接请求

            msg = await query.use_llm_model.requester.invoke_llm(
                query,
                query.use_llm_model,
                req_messages,
                query.use_funcs,
                extra_args=query.use_llm_model.model_entity.extra_args,
                remove_think=remove_think,
            )
            yield msg
            final_msg = msg
        else:
            # 流式输出，需要处理工具调用
            tool_calls_map: dict[str, llm_entities.ToolCall] = {}
            msg_idx = 0
            accumulated_content = ''  # 从开始累积的所有内容
            last_role = 'assistant'
            msg_sequence = 1
            async for msg in query.use_llm_model.requester.invoke_llm_stream(
                query,
                query.use_llm_model,
                req_messages,
                query.use_funcs,
                extra_args=query.use_llm_model.model_entity.extra_args,
                remove_think=remove_think,
            ):
                msg_idx = msg_idx + 1

                # 记录角色
                if msg.role:
                    last_role = msg.role

                # 累积内容
                if msg.content:
                    accumulated_content += msg.content

                # 处理工具调用
                if msg.tool_calls:
                    for tool_call in msg.tool_calls:
                        if tool_call.id not in tool_calls_map:
                            tool_calls_map[tool_call.id] = llm_entities.ToolCall(
                                id=tool_call.id,
                                type=tool_call.type,
                                function=llm_entities.FunctionCall(
                                    name=tool_call.function.name if tool_call.function else '', arguments=''
                                ),
                            )
                        if tool_call.function and tool_call.function.arguments:
                            # 流式处理中，工具调用参数可能分多个chunk返回，需要追加而不是覆盖
                            tool_calls_map[tool_call.id].function.arguments += tool_call.function.arguments
                # 每8个chunk或最后一个chunk时，输出所有累积的内容
                if msg_idx % 8 == 0 or msg.is_final:
                    msg_sequence += 1
                    yield llm_entities.MessageChunk(
                        role=last_role,
                        content=accumulated_content,  # 输出所有累积内容
                        tool_calls=list(tool_calls_map.values()) if (tool_calls_map and msg.is_final) else None,
                        is_final=msg.is_final,
                        msg_sequence=msg_sequence,
                    )

            # 创建最终消息用于后续处理
            final_msg = llm_entities.MessageChunk(
                role=last_role,
                content=accumulated_content,
                tool_calls=list(tool_calls_map.values()) if tool_calls_map else None,
                msg_sequence=msg_sequence,
            )

        pending_tool_calls = final_msg.tool_calls
        first_content = final_msg.content
        if isinstance(final_msg, llm_entities.MessageChunk):

            first_end_sequence = final_msg.msg_sequence

        req_messages.append(final_msg)

        # 持续请求，只要还有待处理的工具调用就继续处理调用
        while pending_tool_calls:
            for tool_call in pending_tool_calls:
                try:
                    func = tool_call.function

                    parameters = json.loads(func.arguments)

                    func_ret = await self.ap.tool_mgr.execute_func_call(query, func.name, parameters)
                    if is_stream:
                        msg = llm_entities.MessageChunk(
                            role='tool',
                            content=json.dumps(func_ret, ensure_ascii=False),
                            tool_call_id=tool_call.id,
                        )
                    else:
                        msg = llm_entities.Message(
                            role='tool',
                            content=json.dumps(func_ret, ensure_ascii=False),
                            tool_call_id=tool_call.id,
                        )

                    yield msg

                    req_messages.append(msg)
                except Exception as e:
                    # 工具调用出错，添加一个报错信息到 req_messages
                    err_msg = llm_entities.Message(role='tool', content=f'err: {e}', tool_call_id=tool_call.id)

                    yield err_msg

                    req_messages.append(err_msg)

            if is_stream:
                tool_calls_map = {}
                msg_idx = 0
                accumulated_content = ''  # 从开始累积的所有内容
                last_role = 'assistant'
                msg_sequence = first_end_sequence

                async for msg in query.use_llm_model.requester.invoke_llm_stream(
                    query,
                    query.use_llm_model,
                    req_messages,
                    query.use_funcs,
                    extra_args=query.use_llm_model.model_entity.extra_args,
                    remove_think=remove_think,
                ):
                    msg_idx += 1

                    # 记录角色
                    if msg.role:
                        last_role = msg.role

                    # 第一次请求工具调用时的内容
                    if msg_idx == 1:
                        accumulated_content = first_content if first_content is not None else accumulated_content

                    # 累积内容
                    if msg.content:
                        accumulated_content += msg.content

                    # 处理工具调用
                    if msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            if tool_call.id not in tool_calls_map:
                                tool_calls_map[tool_call.id] = llm_entities.ToolCall(
                                    id=tool_call.id,
                                    type=tool_call.type,
                                    function=llm_entities.FunctionCall(
                                        name=tool_call.function.name if tool_call.function else '', arguments=''
                                    ),
                                )
                            if tool_call.function and tool_call.function.arguments:
                                # 流式处理中，工具调用参数可能分多个chunk返回，需要追加而不是覆盖
                                tool_calls_map[tool_call.id].function.arguments += tool_call.function.arguments

                    # 每8个chunk或最后一个chunk时，输出所有累积的内容
                    if msg_idx % 8 == 0 or msg.is_final:
                        msg_sequence += 1
                        yield llm_entities.MessageChunk(
                            role=last_role,
                            content=accumulated_content,  # 输出所有累积内容
                            tool_calls=list(tool_calls_map.values()) if (tool_calls_map and msg.is_final) else None,
                            is_final=msg.is_final,
                            msg_sequence=msg_sequence,
                        )

                final_msg = llm_entities.MessageChunk(
                    role=last_role,
                    content=accumulated_content,
                    tool_calls=list(tool_calls_map.values()) if tool_calls_map else None,
                    msg_sequence=msg_sequence,

                )
            else:
                # 处理完所有调用，再次请求
                msg = await query.use_llm_model.requester.invoke_llm(
                    query,
                    query.use_llm_model,
                    req_messages,
                    query.use_funcs,
                    extra_args=query.use_llm_model.model_entity.extra_args,
                    remove_think=remove_think,
                )

                yield msg
                final_msg = msg

            pending_tool_calls = final_msg.tool_calls

            req_messages.append(final_msg)
