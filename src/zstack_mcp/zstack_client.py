"""
ZStack API 客户端 - 处理与 ZStack Cloud 的 API 通信

支持三种认证方式:
1. 用户名密码登录获取 Session
2. 直接传入 SessionID（通过环境变量 ZSTACK_SESSION_ID）
3. AccessKey/SecretKey 请求签名（通过环境变量 ZSTACK_ACCESS_KEY_ID / ZSTACK_ACCESS_KEY_SECRET）

支持:
- 自动登录和 session 管理
- 同步和异步 API 调用
- 异步 API 的 Job 轮询
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, Optional
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx


class ZStackApiError(Exception):
    """ZStack API 错误"""
    def __init__(self, message: str, code: Optional[str] = None, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass
class ZStackSession:
    """ZStack 会话信息"""
    uuid: str
    account_uuid: str = ""
    user_uuid: str = ""
    expire_date: Optional[str] = None


@dataclass(frozen=True)
class ZStackRestRoute:
    """ZStack REST 路由映射。path 不包含 /zstack 前缀。"""
    method: str
    path: str


REST_API_ROUTES: dict[str, ZStackRestRoute] = {
    # 常用只读 Query API，路径来自 zstack-sdk-go-v2 generated actions。
    "QueryAccessKey": ZStackRestRoute("GET", "v1/accesskeys"),
    "QueryAccount": ZStackRestRoute("GET", "v1/accounts"),
    "QueryBackupStorage": ZStackRestRoute("GET", "v1/backup-storage"),
    "QueryCephBackupStorage": ZStackRestRoute("GET", "v1/backup-storage/ceph"),
    "QueryCephPrimaryStorage": ZStackRestRoute("GET", "v1/primary-storage/ceph"),
    "QueryCluster": ZStackRestRoute("GET", "v1/clusters"),
    "QueryDiskOffering": ZStackRestRoute("GET", "v1/disk-offerings"),
    "QueryEip": ZStackRestRoute("GET", "v1/eips"),
    "QueryGlobalConfig": ZStackRestRoute("GET", "v1/global-configurations"),
    "QueryHost": ZStackRestRoute("GET", "v1/hosts"),
    "QueryImage": ZStackRestRoute("GET", "v1/images"),
    "QueryImageStoreBackupStorage": ZStackRestRoute("GET", "v1/backup-storage/image-store"),
    "QueryInstanceOffering": ZStackRestRoute("GET", "v1/instance-offerings"),
    "QueryIpRange": ZStackRestRoute("GET", "v1/l3-networks/ip-ranges"),
    "QueryL2Network": ZStackRestRoute("GET", "v1/l2-networks"),
    "QueryL3Network": ZStackRestRoute("GET", "v1/l3-networks"),
    "QueryLoadBalancer": ZStackRestRoute("GET", "v1/load-balancers"),
    "QueryLoadBalancerListener": ZStackRestRoute("GET", "v1/load-balancers/listeners"),
    "QueryLocalStorageResourceRef": ZStackRestRoute("GET", "v1/primary-storage/local-storage/resource-refs"),
    "QueryLongJob": ZStackRestRoute("GET", "v1/longjobs"),
    "QueryManagementNode": ZStackRestRoute("GET", "v1/management-nodes"),
    "QueryPolicy": ZStackRestRoute("GET", "v1/accounts/policies"),
    "QueryPortForwardingRule": ZStackRestRoute("GET", "v1/port-forwarding"),
    "QueryPrimaryStorage": ZStackRestRoute("GET", "v1/primary-storage"),
    "QueryRole": ZStackRestRoute("GET", "v1/identities/roles"),
    "QuerySecurityGroup": ZStackRestRoute("GET", "v1/security-groups"),
    "QuerySftpBackupStorage": ZStackRestRoute("GET", "v1/backup-storage/sftp"),
    "QuerySystemTag": ZStackRestRoute("GET", "v1/system-tags"),
    "QueryUser": ZStackRestRoute("GET", "v1/accounts/users"),
    "QueryUserTag": ZStackRestRoute("GET", "v1/user-tags"),
    "QueryVip": ZStackRestRoute("GET", "v1/vips"),
    "QueryVirtualRouterOffering": ZStackRestRoute("GET", "v1/instance-offerings/virtual-routers"),
    "QueryVirtualRouterVm": ZStackRestRoute("GET", "v1/vm-instances/appliances/virtual-routers"),
    "QueryVmInstance": ZStackRestRoute("GET", "v1/vm-instances"),
    "QueryVmNic": ZStackRestRoute("GET", "v1/vm-instances/nics"),
    "QueryVolume": ZStackRestRoute("GET", "v1/volumes"),
    "QueryVolumeSnapshot": ZStackRestRoute("GET", "v1/volume-snapshots"),
    "QueryVRouterRouteEntry": ZStackRestRoute("GET", "v1/vrouter-route-tables/route-entries"),
    "QueryVRouterRouteTable": ZStackRestRoute("GET", "v1/vrouter-route-tables"),
    "QueryZone": ZStackRestRoute("GET", "v1/zones"),
}


class ZStackClient:
    """
    ZStack API 客户端
    
    认证方式（按优先级）:
    1. 如果设置了 ZSTACK_SESSION_ID，直接使用该 Session
    2. 如果设置了 ZSTACK_ACCESS_KEY_ID + ZSTACK_ACCESS_KEY_SECRET，使用 AK/SK 签名
    3. 否则使用 ZSTACK_ACCOUNT + ZSTACK_PASSWORD 登录获取 Session
    """
    
    # 轮询 Job 的配置
    JOB_POLL_INTERVAL = 1.0  # 秒
    JOB_POLL_MAX_RETRIES = 300  # 最多轮询次数（5分钟）
    
    def __init__(
        self,
        api_url: Optional[str] = None,
        account: Optional[str] = None,
        password: Optional[str] = None,
        session_id: Optional[str] = None,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
    ):
        """
        初始化 ZStack 客户端
        
        Args:
            api_url: ZStack API 地址，如 http://localhost:8080
            account: 账户名（用户名密码认证时使用）
            password: 密码（明文，会自动进行 SHA512 加密）
            session_id: 直接传入的 Session UUID（优先级高于用户名密码）
            access_key_id: AccessKey ID（AK/SK 认证时使用）
            access_key_secret: AccessKey Secret（AK/SK 认证时使用）
        """
        self.api_url = api_url or os.environ.get('ZSTACK_API_URL', 'http://localhost:8080')
        self.account = account or os.environ.get('ZSTACK_ACCOUNT', 'admin')
        self.password = password or os.environ.get('ZSTACK_PASSWORD', '')
        self.access_key_id = (
            access_key_id
            or os.environ.get('ZSTACK_ACCESS_KEY_ID', '')
            or os.environ.get('ZSTACK_AK', '')
        )
        self.access_key_secret = (
            access_key_secret
            or os.environ.get('ZSTACK_ACCESS_KEY_SECRET', '')
            or os.environ.get('ZSTACK_SK', '')
        )
        
        # 显式传入 AK/SK 时不再回退环境变量里的 session，避免 HTTP 头凭据被进程级 session 覆盖。
        explicit_access_key = access_key_id is not None or access_key_secret is not None
        if session_id is not None:
            env_session_id = session_id
        elif explicit_access_key:
            env_session_id = ''
        else:
            env_session_id = os.environ.get('ZSTACK_SESSION_ID', '')
        
        # 如果有 session_id，直接创建 session 对象
        if env_session_id:
            self.session: Optional[ZStackSession] = ZStackSession(uuid=env_session_id)
        else:
            self.session = None
        
        self._http_client: Optional[httpx.AsyncClient] = None
    
    @property
    def api_endpoint(self) -> str:
        """API 端点地址"""
        return f"{self.api_url.rstrip('/')}/zstack/api/"
    
    @property
    def auth_mode(self) -> str:
        """当前认证模式"""
        if self.session and self.session.uuid:
            if not self.session.account_uuid:
                return "session_id"  # 直接传入的 session
            return "session"  # 登录获取的 session
        if self.access_key_id or self.access_key_secret:
            return "access_key"
        return "password"  # 需要密码登录
    
    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端（懒加载）"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client
    
    async def logout(self) -> None:
        """调用 LogOut API 销毁当前 session，然后关闭 HTTP 客户端"""
        if self.auth_mode == "access_key":
            await self.close()
            return
        if self.session and self.session.uuid:
            try:
                await self.execute(
                    "LogOut",
                    "org.zstack.header.identity.APILogOutMsg",
                    {"sessionUuid": self.session.uuid},
                )
            except Exception:
                pass  # best-effort，忽略 logout 失败
            self.session = None
        await self.close()

    async def close(self) -> None:
        """关闭客户端"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
    
    @staticmethod
    def _sha512(text: str) -> str:
        """SHA512 加密"""
        return hashlib.sha512(text.encode('utf-8')).hexdigest()

    @staticmethod
    def _format_access_key_date() -> str:
        """返回 ZStack AK/SK 签名使用的本地时区 Date 字符串。"""
        now = datetime.now().astimezone()
        zone_name = now.strftime("%Z")
        if zone_name:
            return now.strftime("%a, %d %b %Y %H:%M:%S %Z")
        return format_datetime(datetime.now(timezone.utc), usegmt=True)

    @staticmethod
    def _canonical_access_key_uri(url: str) -> str:
        """按 ZStack Go SDK 规则提取签名用 URI：去掉 /zstack context path 和 query。"""
        parsed = urlparse(url)
        path = parsed.path or "/"
        context_path = "/zstack"
        idx = path.find(context_path)
        if idx >= 0:
            uri = path[idx + len(context_path):]
            return uri or "/"
        return path

    def _validate_access_key(self) -> None:
        if not self.access_key_id or not self.access_key_secret:
            raise ZStackApiError(
                "AK/SK 未配置完整，请同时设置 ZSTACK_ACCESS_KEY_ID 和 "
                "ZSTACK_ACCESS_KEY_SECRET，或通过 HTTP 头传入 "
                "X-ZStack-Access-Key-Id / X-ZStack-Access-Key-Secret"
            )

    def _access_key_auth_headers(
        self,
        method: str,
        url: str,
        date: Optional[str] = None,
    ) -> dict[str, str]:
        """生成 ZStack AK/SK 请求签名头。

        签名算法参考 zstack-sdk-go-v2:
        base64(hmac-sha1(secret, METHOD + "\n" + Date + "\n" + uri))
        """
        self._validate_access_key()
        date = date or self._format_access_key_date()
        method = method.upper()
        uri = self._canonical_access_key_uri(url)
        string_to_sign = f"{method}\n{date}\n{uri}"
        digest = hmac.new(
            self.access_key_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        return {
            "Authorization": f"ZStack {self.access_key_id}:{signature}",
            "Date": date,
        }

    def _request_headers(self, method: str, url: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth_mode == "access_key":
            headers.update(self._access_key_auth_headers(method, url))
        return headers

    def _rest_url(self, path: str) -> str:
        return f"{self.api_url.rstrip('/')}/zstack/{path.lstrip('/')}"

    @staticmethod
    def _rest_query_value(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value)

    @classmethod
    def _rest_condition_to_q(cls, condition: Any) -> Optional[str]:
        if isinstance(condition, str):
            return condition.strip() or None
        if not isinstance(condition, dict):
            return None
        name = condition.get("name")
        value = condition.get("value")
        if name is None or value is None:
            return None
        op = str(condition.get("op") or "=").strip()
        if op == "==":
            op = "="
        return f"{name}{op}{cls._rest_query_value(value)}"

    @classmethod
    def _rest_query_params(cls, parameters: dict[str, Any]) -> dict[str, Any]:
        query: dict[str, Any] = {}
        passthrough_keys = (
            "limit",
            "start",
            "replyWithCount",
            "count",
            "groupBy",
            "filterName",
            "sort",
        )
        for key in passthrough_keys:
            if key in parameters and parameters[key] is not None:
                query[key] = cls._rest_query_value(parameters[key])

        fields = parameters.get("fields")
        if fields:
            query["fields"] = cls._rest_query_value(fields)

        q_values: list[str] = []
        raw_q = parameters.get("q")
        if isinstance(raw_q, str):
            q_values.append(raw_q)
        elif isinstance(raw_q, (list, tuple, set)):
            q_values.extend(str(item) for item in raw_q if item is not None)

        conditions = parameters.get("conditions")
        if isinstance(conditions, dict):
            conditions = [conditions]
        if isinstance(conditions, (list, tuple)):
            for condition in conditions:
                q = cls._rest_condition_to_q(condition)
                if q:
                    q_values.append(q)
        if q_values:
            query["q"] = q_values

        return query

    def _rest_route_for_api(self, api_name: str) -> ZStackRestRoute:
        route = REST_API_ROUTES.get(api_name)
        if route is None:
            raise ZStackApiError(
                message=(
                    f"AK/SK 认证不支持 /zstack/api/ message API，且当前未配置 "
                    f"{api_name} 的 REST 路由映射"
                ),
                code="REST_MAPPING_NOT_FOUND",
                details={
                    "apiName": api_name,
                    "authMode": "access_key",
                    "hint": "请为该 API 增加 REST path/method 映射，或改用账号密码/session 认证。",
                },
            )
        return route

    async def execute_rest(
        self,
        api_name: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """使用 REST API 执行 AK/SK 请求。"""
        route = self._rest_route_for_api(api_name)
        if route.method != "GET":
            raise ZStackApiError(
                message=f"AK/SK REST 路由 {api_name} 的方法 {route.method} 暂未实现",
                code="REST_METHOD_NOT_IMPLEMENTED",
                details={"apiName": api_name, "method": route.method, "path": route.path},
            )

        url = self._rest_url(route.path)
        query = self._rest_query_params(parameters)
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"

        client = await self._get_http_client()
        response = await client.get(
            url,
            headers=self._request_headers(route.method, url),
        )
        if response.status_code >= 400:
            raise ZStackApiError(
                message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                code=str(response.status_code),
            )

        try:
            result = response.json()
        except Exception as e:
            raise ZStackApiError(
                message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
            )

        if isinstance(result, list):
            return {"inventories": result}
        if isinstance(result, dict):
            if "error" in result:
                error = result["error"]
                if isinstance(error, dict):
                    raise ZStackApiError(
                        message=error.get("description", "请求失败"),
                        code=error.get("code"),
                        details=error,
                    )
                raise ZStackApiError(message=str(error or "请求失败"))
            return result
        return {"raw": result}

    @staticmethod
    def _normalize_metric_time(value: Any) -> Any:
        """将时间规范化为秒级时间戳（支持 ISO 字符串/毫秒/秒）"""
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 10_000_000_000:
                return int(value // 1000)
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                num = int(text)
                if num > 10_000_000_000:
                    num = num // 1000
                return num
            try:
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                if " " in text and "T" not in text:
                    text = text.replace(" ", "T")
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                return value
        return value

    @staticmethod
    def _normalize_metric_period(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return value

    @staticmethod
    def _normalize_metric_labels(labels: Any) -> Optional[list[str]]:
        """统一 labels 为字符串列表，支持 dict / list[str] / list[dict]"""
        if labels is None:
            return None
        if isinstance(labels, dict):
            return [f"{key}={value}" for key, value in labels.items()]
        if isinstance(labels, (list, tuple, set)):
            results: list[str] = []
            for item in labels:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        results.append(text)
                    continue
                if isinstance(item, dict):
                    key = item.get("key") or item.get("name")
                    if key is not None and "value" in item:
                        results.append(f"{key}={item.get('value')}")
                        continue
                    if key is not None and "val" in item:
                        results.append(f"{key}={item.get('val')}")
                        continue
                    if "label" in item:
                        results.append(str(item.get("label")))
                        continue
                results.append(str(item))
            return results or None
        return [str(labels)]
    
    def _parse_response(self, response_data: dict) -> dict[str, Any]:
        """
        解析 ZStack API 响应
        
        ZStack 返回格式有两种:
        1. 直接返回: {"org.zstack.xxx.Reply": {...}}
        2. 包装返回: {"state": "Done", "result": "{...json string...}"}
        
        Args:
            response_data: API 响应 JSON
            
        Returns:
            解析后的实际数据
        """
        # 检查是否是包装格式
        if 'state' in response_data and 'result' in response_data:
            state = response_data.get('state')
            
            # 检查状态
            if state == 'Error' or 'error' in response_data:
                error_msg = response_data.get('error', {})
                if isinstance(error_msg, str):
                    raise ZStackApiError(message=error_msg)
                raise ZStackApiError(
                    message=error_msg.get('description', '请求失败'),
                    code=error_msg.get('code'),
                    details=error_msg
                )
            
            # 解析 result JSON 字符串
            result_str = response_data.get('result', '{}')
            if isinstance(result_str, str):
                try:
                    result = json.loads(result_str)
                except json.JSONDecodeError:
                    return {'raw': result_str}
            else:
                result = result_str
            
            # 提取实际数据
            if result:
                reply_key = list(result.keys())[0]
                reply_data = result[reply_key]
                self._raise_if_reply_error(reply_data)
                return reply_data
            return result
        
        # 直接格式：检查错误
        if 'error' in response_data:
            error = response_data['error']
            raise ZStackApiError(
                message=error.get('description', '请求失败'),
                code=error.get('code'),
                details=error
            )
        
        # 提取实际数据
        if response_data:
            reply_key = list(response_data.keys())[0]
            reply_data = response_data[reply_key]
            self._raise_if_reply_error(reply_data)
            return reply_data
        
        return response_data

    @staticmethod
    def _raise_if_reply_error(reply_data: Any) -> None:
        if not isinstance(reply_data, dict):
            return
        if reply_data.get('success') is False and 'error' in reply_data:
            error = reply_data.get('error')
            if isinstance(error, dict):
                raise ZStackApiError(
                    message=error.get('description', '请求失败'),
                    code=error.get('code'),
                    details=error,
                )
            raise ZStackApiError(message=str(error or '请求失败'))

    @staticmethod
    def _is_session_invalid_error(error: ZStackApiError) -> bool:
        if not error:
            return False
        code = getattr(error, 'code', None)
        details = getattr(error, 'details', None)
        if code in ('ID.1001', 'ORG_ZSTACK_IDENTITY_10020'):
            return True
        if isinstance(details, dict):
            if details.get('code') in ('ID.1001', 'ORG_ZSTACK_IDENTITY_10020'):
                return True
            if details.get('globalErrorCode') in ('ORG_ZSTACK_IDENTITY_10020',):
                return True
            detail_text = str(details.get('details') or details.get('description') or '').lower()
            if 'session' in detail_text and ('invalid' in detail_text or 'expired' in detail_text):
                return True
        message = str(error).lower()
        return 'session' in message and ('invalid' in message or 'expired' in message)

    def _can_refresh_session(self) -> bool:
        if self.auth_mode in ("session_id", "access_key"):
            return False
        return bool(self.password)

    async def _refresh_session(self) -> None:
        self.session = None
        await self.login()
    
    async def login(self) -> ZStackSession:
        """
        登录 ZStack 获取 session
        
        Returns:
            ZStackSession 对象
        """
        if self.auth_mode == "access_key":
            raise ZStackApiError("AK/SK 认证不需要登录，请直接执行 API")
        if not self.password:
            raise ZStackApiError(
                "密码未配置，请设置 ZSTACK_PASSWORD 环境变量，"
                "或设置 ZSTACK_SESSION_ID 直接使用已有会话，"
                "或设置 ZSTACK_ACCESS_KEY_ID + ZSTACK_ACCESS_KEY_SECRET 使用 AK/SK 认证"
            )
        
        password_hash = self._sha512(self.password)
        
        request_body = {
            "org.zstack.header.identity.APILogInByAccountMsg": {
                "accountName": self.account,
                "password": password_hash,
            }
        }
        
        client = await self._get_http_client()
        response = await client.post(
            self.api_endpoint,
            json=request_body,
            headers=self._request_headers("POST", self.api_endpoint)
        )
        
        # 检查 HTTP 状态码
        if response.status_code >= 400:
            raise ZStackApiError(
                message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                code=str(response.status_code),
            )
        
        try:
            result = response.json()
        except Exception as e:
            raise ZStackApiError(
                message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
            )
        
        reply_data = self._parse_response(result)
        
        # 提取 session 信息
        session_data = reply_data.get('inventory', {})
        
        self.session = ZStackSession(
            uuid=session_data.get('uuid', ''),
            account_uuid=session_data.get('accountUuid', ''),
            user_uuid=session_data.get('userUuid', ''),
            expire_date=session_data.get('expiredDate'),
        )
        
        return self.session
    
    async def ensure_session(self) -> ZStackSession:
        """确保有有效的 session，如果没有则登录"""
        if self.session is None:
            await self.login()
        return self.session  # type: ignore
    
    async def execute(
        self,
        api_name: str,
        full_api_name: str,
        parameters: dict[str, Any],
        is_async: bool = False,
    ) -> dict[str, Any]:
        """
        执行 ZStack API
        
        Args:
            api_name: API 简称，如 QueryVmInstance
            full_api_name: 完整 API 名称，如 org.zstack.header.vm.APIQueryVmInstanceMsg
            parameters: API 参数
            is_async: 是否为异步 API
            
        Returns:
            API 返回结果
        """
        if self.auth_mode == "access_key":
            return await self.execute_rest(api_name, parameters)

        base_parameters = dict(parameters)

        async def send_once() -> dict[str, Any]:
            # 确保已登录（除了登录 API 本身）
            if self.auth_mode == "access_key":
                self._validate_access_key()
                request_parameters = base_parameters
            elif 'LogIn' not in api_name:
                session = await self.ensure_session()
                # 添加 session 信息
                request_parameters = {
                    **base_parameters,
                    "session": {"uuid": session.uuid}
                }
            else:
                request_parameters = base_parameters

            # 构建请求体
            request_body = {
                full_api_name: request_parameters
            }
            
            client = await self._get_http_client()
            response = await client.post(
                self.api_endpoint,
                json=request_body,
                headers=self._request_headers("POST", self.api_endpoint)
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            # 检查是否需要轮询 Job（异步 API）
            # 包装格式下，如果 state 不是 Done，需要轮询
            if 'state' in result:
                state = result.get('state')
                if state not in ('Done', 'Error'):
                    # 需要轮询
                    location = result.get('location')
                    if not location and result.get('uuid'):
                        location = f"{self.api_endpoint}result/{result['uuid']}"
                    if location:
                        return await self._poll_job(location)
            
            return self._parse_response(result)

        try:
            return await send_once()
        except ZStackApiError as e:
            if (
                'LogIn' not in api_name
                and self._is_session_invalid_error(e)
                and self._can_refresh_session()
            ):
                await self._refresh_session()
                return await send_once()
            raise
    
    async def _poll_job(self, job_location: str) -> dict[str, Any]:
        """
        轮询异步 Job 直到完成
        
        Args:
            job_location: Job 查询地址
            
        Returns:
            Job 的最终结果
        """
        client = await self._get_http_client()
        
        for _ in range(self.JOB_POLL_MAX_RETRIES):
            await asyncio.sleep(self.JOB_POLL_INTERVAL)
            
            response = await client.get(
                job_location,
                headers=self._request_headers("GET", job_location)
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"Job 查询失败, HTTP {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"Job 响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            # 检查状态
            if 'state' in result:
                state = result.get('state')
                if state == 'Done':
                    return self._parse_response(result)
                elif state == 'Error':
                    error = result.get('error', {})
                    raise ZStackApiError(
                        message=error.get('description', 'Job 执行失败') if isinstance(error, dict) else str(error),
                        code=error.get('code') if isinstance(error, dict) else None,
                        details=error if isinstance(error, dict) else None
                    )
                # 其他状态继续轮询
            else:
                # 非包装格式，直接返回
                return self._parse_response(result)
        
        raise ZStackApiError("Job 执行超时，请稍后重试")
    
    async def query_metric_data(
        self,
        namespace: str,
        metric_name: str,
        start_time: Any = None,
        end_time: Any = None,
        period: Any = 60,
        labels: Any = None,
    ) -> dict[str, Any]:
        """
        查询监控数据
        
        使用 ZStack 的 GetMetricData API
        
        Args:
            namespace: 命名空间，如 ZStack/VM
            metric_name: 指标名称
            start_time: 开始时间（ISO 或秒级时间戳）
            end_time: 结束时间（ISO 或秒级时间戳）
            period: 采样周期（秒）
            labels: 标签过滤，如 ["VMUuid=xxx"] 或 {"VMUuid":"xxx"}
            
        Returns:
            监控数据
        """
        start_time = self._normalize_metric_time(start_time)
        end_time = self._normalize_metric_time(end_time)
        period = self._normalize_metric_period(period)
        labels = self._normalize_metric_labels(labels)

        async def send_once() -> dict[str, Any]:
            if self.auth_mode == "access_key":
                raise ZStackApiError(
                    message=(
                        "AK/SK 认证不支持 /zstack/api/ message API，"
                        "get_metric_data 暂未配置 REST 路由映射"
                    ),
                    code="REST_MAPPING_NOT_FOUND",
                    details={
                        "apiName": "GetMetricData",
                        "authMode": "access_key",
                        "hint": "请为 GetMetricData 增加 REST path/method/parameter 映射，或改用账号密码/session 认证。",
                    },
                )
            payload = {
                "namespace": namespace,
                "metricName": metric_name,
                "startTime": start_time,
                "endTime": end_time,
                "period": period,
                "labels": labels,
            }
            session = await self.ensure_session()
            payload["session"] = {"uuid": session.uuid}
            payload = {key: value for key, value in payload.items() if value is not None}
            request_body = {
                "org.zstack.zwatch.api.APIGetMetricDataMsg": payload
            }
            
            client = await self._get_http_client()
            response = await client.post(
                self.api_endpoint,
                json=request_body,
                headers=self._request_headers("POST", self.api_endpoint)
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            return self._parse_response(result)

        try:
            return await send_once()
        except ZStackApiError as e:
            if self._is_session_invalid_error(e) and self._can_refresh_session():
                await self._refresh_session()
                return await send_once()
            raise
