# encoding:utf-8

import time

import openai
import openai.error
import requests

from bot.bot import Bot
from bot.chatgpt.chat_gpt_session import ChatGPTSession
from bot.openai.open_ai_image import OpenAIImage
from bot.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.token_bucket import TokenBucket
from config import conf, load_config
from datetime import datetime
from common import chat_prompt


# OpenAI对话模型API (可用)
class ChatGPTBot(Bot, OpenAIImage):
    def __init__(self):
        super().__init__()
        # set the default api_key
        openai.api_key = conf().get("open_ai_api_key")
        if conf().get("open_ai_api_base"):
            openai.api_base = conf().get("open_ai_api_base")
        proxy = conf().get("proxy")
        if proxy:
            openai.proxy = proxy
        if conf().get("rate_limit_chatgpt"):
            self.tb4chatgpt = TokenBucket(conf().get("rate_limit_chatgpt", 20))

        self.sessions = SessionManager(ChatGPTSession, model=conf().get("model") or "gpt-3.5-turbo")
        self.args = {
            "model": conf().get("model") or "gpt-3.5-turbo",  # 对话模型的名称
            "temperature": conf().get("temperature", 0.9),  # 值在[0,1]之间，越大表示回复越具有不确定性
            # "max_tokens":4096,  # 回复最大的字符数
            "top_p": conf().get("top_p", 1),
            "frequency_penalty": conf().get("frequency_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "presence_penalty": conf().get("presence_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "request_timeout": conf().get("request_timeout", None),  # 请求超时时间，openai接口默认设置为600，对于难问题一般需要较长时间
            "timeout": conf().get("request_timeout", None),  # 重试超时时间，在这个时间内，将会自动重试
        }

    def reply(self, query, context=None):
        create_time = datetime.now()
        is_add_session = True

        # acquire reply content
        if context.type == ContextType.TEXT:
            session_id = context["session_id"]
            receiver_name = context.kwargs.get("receiver_name", "")
            logger.info(f"[CHATGPT] session_id={session_id} query={query} by={receiver_name}")

            reply = None

            clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
            if query in clear_memory_commands:
                self.sessions.clear_session(session_id)
                reply = Reply(ReplyType.INFO, "记忆已清除")
            elif query == "#清除所有":
                self.sessions.clear_all_session()
                reply = Reply(ReplyType.INFO, "所有人记忆已清除")
            elif query == "#更新配置":
                load_config()
                reply = Reply(ReplyType.INFO, "配置已更新")

            if reply:
                return reply

            api_key = context.get("openai_api_key")
            model = context.get("gpt_model")
            new_args = None
            if model:
                new_args = self.args.copy()
                new_args["model"] = model

            if query.startswith(chat_prompt.acrostic_poem_keywords):
                # 创建新的session
                session = ChatGPTSession(session_id, system_prompt=chat_prompt.acrostic_poem, model=model)
                query = query.replace(chat_prompt.acrostic_poem_keywords, "", 1)
                session.add_query(query)
                is_add_session = False
            elif query.startswith(chat_prompt.greeting_keywords):
                # 创建新的session
                session = ChatGPTSession(session_id, system_prompt=chat_prompt.greeting, model=model)
                query = query.replace(chat_prompt.greeting_keywords, "", 1)
                session.add_query(query)
                is_add_session = False
            else:
                session = self.sessions.session_query(query, session_id)
                logger.debug("[CHATGPT] session query={}".format(session.messages))

            reply_content = self.reply_text(session, api_key, args=new_args)
            logger.debug(
                "[CHATGPT] new_query={}, session_id={}, reply_cont={}, completion_tokens={}, model={}".format(
                    session.messages,
                    session_id,
                    reply_content["content"],
                    reply_content["completion_tokens"],
                    reply_content["model_type"]
                )
            )

            if reply_content["completion_tokens"] == 0 and len(reply_content["content"]) > 0:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
            elif reply_content["completion_tokens"] > 0:
                if is_add_session:
                    self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
                reply_content["create_time"] = create_time.strftime("%Y-%m-%d %H:%M:%S")
                reply_content["complete_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                reply = Reply(ReplyType.TEXT, reply_content["content"], kwargs=reply_content)
            else:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
                logger.debug("[CHATGPT] reply {} used 0 tokens.".format(reply_content))

            return reply

        elif context.type == ContextType.IMAGE_CREATE:
            ok, retstring = self.create_img(query, 0)
            reply = None
            if ok:
                reply_content = {"create_time": create_time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "complete_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                reply = Reply(ReplyType.IMAGE_URL, retstring, reply_content)
            else:
                reply = Reply(ReplyType.ERROR, retstring)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    def reply_text(self, session: ChatGPTSession, api_key=None, args=None, retry_count=0) -> dict:
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """
        try:
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise openai.error.RateLimitError("RateLimitError: rate limit exceeded")
            # if api_key == None, the default openai.api_key will be used
            if args is None:
                args = self.args
            response = openai.ChatCompletion.create(api_key=api_key, messages=session.messages, **args)
            # logger.debug("[CHATGPT] response={}".format(response))
            # logger.info("[ChatGPT] reply={}, total_tokens={}".format(response.choices[0]['message']['content'], response["usage"]["total_tokens"]))
            return {
                "model_type": response.engine,
                "total_tokens": response["usage"]["total_tokens"],
                "prompt_tokens": response["usage"]["prompt_tokens"],
                "completion_tokens": response["usage"]["completion_tokens"],
                "content": response.choices[0]["message"]["content"],
            }
        except Exception as e:
            need_retry = retry_count < 2
            result = {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}
            if isinstance(e, openai.error.RateLimitError):
                logger.warn("[CHATGPT] RateLimitError: {}".format(e))
                result["content"] = "提问太快啦，请休息一下再问我吧"
                if need_retry:
                    time.sleep(20)
            elif isinstance(e, openai.error.Timeout):
                logger.warn("[CHATGPT] Timeout: {}".format(e))
                result["content"] = "我没有收到你的消息"
                if need_retry:
                    time.sleep(5)
            elif isinstance(e, openai.error.APIError):
                logger.warn("[CHATGPT] Bad Gateway: {}".format(e))
                result["content"] = "请再问我一次"
                if need_retry:
                    time.sleep(10)
            elif isinstance(e, openai.error.APIConnectionError):
                logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
                result["content"] = "我连接不到你的网络"
                if need_retry:
                    time.sleep(5)
            else:
                logger.exception("[CHATGPT] Exception: {}".format(e))
                need_retry = False
                self.sessions.clear_session(session.session_id)

            if need_retry:
                logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
                return self.reply_text(session, api_key, args, retry_count + 1)
            else:
                return result


class AzureChatGPTBot(ChatGPTBot):
    def __init__(self):
        super().__init__()
        openai.api_type = "azure"
        openai.api_version = conf().get("azure_api_version", "2023-06-01-preview")
        self.args["deployment_id"] = conf().get("azure_deployment_id")

    def create_img(self, query, retry_count=0, api_key=None):
        url = "{}/openai/deployments/Dalle3/images/generations?api-version=2023-12-01-preview".format(openai.api_base)
        logger.info("azure openai image base url: {}".format(url))
        api_key = api_key or openai.api_key
        headers = {"api-key": api_key, "Content-Type": "application/json"}

        quality = "standard"  # Options are “hd” and “standard”; defaults to standard

        if query.startswith(chat_prompt.art_toy_keywords):
            quality = "hd"
            query = chat_prompt.art_toy + query.replace(chat_prompt.art_toy_keywords, "", 1)

        try:
            body = {
                # Enter your prompt text here
                "prompt": query,
                "size": "1024x1024",  # supported values are “1024x1024”
                "n": 1,
                "quality": quality,  # Options are “hd” and “standard”; defaults to standard
                "style": "vivid"  # Options are “natural” and “vivid”; defaults to “vivid”
            }
            logger.info("azure openai image request body: {}".format(body))
            submission = requests.post(url, headers=headers, json=body)
            response_json = submission.json()

            logger.info("azure openai image response json: {}".format(response_json))

            if 'data' in response_json and response_json['data']:
                # 正常返回，提取 URL
                image_url = response_json['data'][0]['url']
                return True, image_url
            elif 'error' in response_json:
                # 错误返回，提取错误代码
                error_code = response_json['error'].get('code')
                if error_code == "contentFilter":
                    # 任务失败，可能是因为内容不符合规范
                    return False, "提示语内容不符合安全审查，请重新组织提示语"
                elif error_code == "tooManyRequests":
                    # 任务失败，可能是因为系统繁忙
                    return False, "系统繁忙，请稍后再试"

                return False, "图片生成失败"
            else:
                # 未知返回类型
                return False, "图片生成失败"

        except Exception as e:
            logger.error("create image error: {}".format(e))
            return False, "图片生成失败"
