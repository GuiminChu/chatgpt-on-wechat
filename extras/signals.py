from blinker import signal
from bridge.context import Context, ContextType
from bridge.reply import Reply
from channel.chat_message import ChatMessage
from common.log import logger
import requests

from config import conf

custom_callback_signal = signal('custom_callback_signal')
request_url = conf().get("call_back_url")


@custom_callback_signal.connect
def handle_custom_signal(sender, **kwargs):
    print(f"Received signal from {sender}, data: {kwargs}")

    context: Context = kwargs.get("context", None)
    chat_message: ChatMessage = context.kwargs.get("msg", None)
    reply: Reply = kwargs.get("reply", None)

    if context is None or chat_message is None or reply is None:
        return

    try:
        if request_url is not None:
            body = {
                "groupId": context.kwargs.get("receiver", ""),
                "groupName": context.kwargs.get("receiver_name", ""),
                "messageQuery": context.content,
                "usageTokens": reply.kwargs.get("completion_tokens", 0),
                "totalTokens": reply.kwargs.get("total_tokens", 0),
                "queryUserId": chat_message.actual_user_id,
                "queryUserNickname": chat_message.actual_user_nickname,
                "askTime": reply.kwargs.get("create_time", ""),
                "completeTime": reply.kwargs.get("complete_time", "")
            }

            if context.type == ContextType.TEXT:
                body["contentType"] = "text"
                body["messageContent"] = reply.kwargs.get("content", "")
                body["modelType"] = reply.kwargs.get("model_type", "")
            elif context.type == ContextType.IMAGE_CREATE:
                body["contentType"] = "image"
                body["messageContent"] = reply.content
                body["modelType"] = "Dalle3"

            response = requests.post(request_url, json=body)
            if response.status_code == 200:
                print(response.json())
            else:
                logger.debug("[SYS] request callback url 响应状态码: {}".format(response.status_code))
                logger.debug("[SYS] request callback url 响应内容: {}".format(response.text))
    except Exception as e:
        logger.error("[SYS] request callback url error: {}".format(str(e)))
