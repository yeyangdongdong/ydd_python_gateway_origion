"""HTTP 请求/响应模型与页面路径；只放数据形状，不放业务逻辑。"""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

AGENT_PAGE = 100
SESSION_PAGE = 100
MESSAGE_PAGE = 1000

CHAT_PAGE = Path(__file__).resolve().parent.parent.parent / "web" / "chat.html"
LOGIN_PAGE = Path(__file__).resolve().parent.parent.parent / "web" / "login.html"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentCreate(InputModel):
    name: Text = Field(max_length=100)
    system_prompt: Text = Field(default="你是一个友好、准确的中文助手。", max_length=16000)
    model: Text | None = Field(default=None, max_length=200)


class AgentUpdate(InputModel):
    # None 表示这一项不修改；全部省略会被视为无效请求。
    name: Text | None = Field(default=None, max_length=100)
    system_prompt: Text | None = Field(default=None, max_length=16000)
    model: Text | None = Field(default=None, max_length=200)


class SessionCreate(InputModel):
    agent_id: Text = Field(default="default", max_length=100)
    title: Text = Field(default="新会话", max_length=200)


class SessionUpdate(InputModel):
    title: Text = Field(max_length=200)


class ChatInput(InputModel):
    content: Text = Field(max_length=32000)
    request_id: Text = Field(default_factory=lambda: uuid4().hex, max_length=100)


class ProxyTicketInput(InputModel):
    # 要连接的代理 WS 路径，必须落在已配置的前缀下。
    path: Text = Field(max_length=2000)


class ApiKeyCreate(InputModel):
    name: Text = Field(max_length=100)
    scopes: list[str] = Field(default_factory=list, max_length=10)
    # 不传则用服务端默认天数；0 表示永不过期。
    expires_in_days: int | None = Field(default=None, ge=0, le=3650)


class UserCreate(InputModel):
    username: Text = Field(max_length=50)
    password: Text = Field(max_length=200)
    role: str = Field(default="member", pattern="^(admin|member|viewer)$")


class UserUpdate(InputModel):
    role: str | None = Field(default=None, pattern="^(admin|member|viewer)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class PasswordReset(InputModel):
    password: Text = Field(max_length=200)


class PasswordChange(InputModel):
    current_password: Text = Field(max_length=200)
    new_password: Text = Field(max_length=200)
