# Module 2：pm_gateway 认证模块 — 设计文档

> **版本**: v1.0
> **日期**: 2026-02-20
> **状态**: 已确认，待实施
> **对齐文档**:
> - `Planning/Detail_Design/01_全局约定与数据库设计.md` v2.3
> - `Planning/Detail_Design/02_API接口契约.md` v1.2
> **实施计划**: `2026-02-20-pm-gateway-plan.md`

---

## 一、模块职责

`pm_gateway` 负责用户注册、登录认证和 JWT Token 管理。是其他所有需要认证接口的依赖基础。

**包含**：
- 用户注册（同时创建 accounts 行）
- 用户名/密码登录（签发双 Token）
- Refresh Token 刷新（换发新 Access Token）
- `get_current_user` 依赖注入（供其他模块使用）
- 请求日志中间件（基础实现）
- 限流中间件（骨架 + TODO）

**不包含**：
- 角色/权限系统（MVP 暂不做，所有认证用户权限相同）
- 邮箱验证流程
- 登出/Token 主动吊销（无状态 JWT 不支持，TODO 注释说明）
- OAuth 2.0 完整协议（仅借用 Bearer Token 约定）

---

## 二、技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| JWT 签发/验证 | `python-jose[cryptography]` | 已在 pyproject.toml，HS256 算法 |
| Token 提取依赖 | FastAPI `OAuth2PasswordBearer` | 自动从 `Authorization: Bearer` 头提取 |
| 密码哈希 | `passlib[bcrypt]` | 已在 pyproject.toml |
| 请求日志 | Python 标准 `logging` | 无额外依赖 |

**MVP 简化说明**（代码中需加注释）：
- HS256（对称）→ 生产环境升级为 RS256（非对称，public key 可分发）
- 无 refresh token rotation → 生产环境每次刷新同时换发新 refresh_token
- 无 token 吊销列表 → 生产环境引入 Redis 黑名单或 jti claim
- passlib bcrypt → 生产环境可升级为 Argon2

---

## 三、文件结构

```
src/pm_gateway/
├── __init__.py
├── auth/
│   ├── __init__.py
│   ├── jwt_handler.py      # JWT 签发/验证核心逻辑
│   ├── password.py         # bcrypt hash/verify
│   └── dependencies.py     # get_current_user Depends()
├── user/
│   ├── __init__.py
│   ├── db_models.py        # UserModel (SQLAlchemy ORM)
│   ├── schemas.py          # Pydantic 请求/响应 Schema
│   └── service.py          # register / login 业务逻辑
├── middleware/
│   ├── __init__.py
│   ├── rate_limit.py       # TODO 骨架，不实现
│   └── request_log.py      # 基础请求日志（~15行）
└── api/
    ├── __init__.py
    └── router.py           # 3 个端点
```

---

## 四、JWT 设计

### Token 结构

```python
# Access Token Payload
{
    "sub": "<user_id (UUID as str)>",
    "type": "access",
    "exp": <now + 30min>,
    "iat": <now>
}

# Refresh Token Payload
{
    "sub": "<user_id (UUID as str)>",
    "type": "refresh",
    "exp": <now + 7d>,
    "iat": <now>
}
```

### 关键安全规则

- `type` claim 必须严格校验：refresh token 不能用于访问受保护接口，access token 不能用于刷新
- Secret Key 从 `settings.SECRET_KEY` 读取（env 变量），不硬编码
- 算法固定为 HS256，decode 时必须指定 `algorithms=["HS256"]` 防算法混淆攻击

### 核心函数签名

```python
# jwt_handler.py
def create_access_token(user_id: str) -> str: ...
def create_refresh_token(user_id: str) -> str: ...
def decode_token(token: str, expected_type: str) -> dict[str, str]:
    """
    验证 token 签名、过期时间、type claim。
    失败统一抛出 InvalidRefreshTokenError 或 401（由调用方决定）。
    """
```

---

## 五、Service 层逻辑

### register

```
输入: username, email, password (plain)
1. SELECT users WHERE username = ? → 存在则 UsernameExistsError(1001)
2. SELECT users WHERE email = ?    → 存在则 EmailExistsError(1002)
3. hash_password(password)
4. 开启事务:
   a. INSERT INTO users (username, email, password_hash)
   b. INSERT INTO accounts (user_id, available_balance=0, frozen_balance=0, version=0)
5. 返回 UserModel
```

> 注意：步骤 1/2 的唯一性检查在事务外，存在极小概率竞争条件（并发注册同名）。
> 数据库的 UNIQUE 约束是最终防线，捕获 IntegrityError 并转换为对应 AppError。

### login

```
输入: username, password (plain)
1. SELECT users WHERE username = ? → 不存在则 InvalidCredentialsError(1003)
2. verify_password(plain, hash)    → 失败则 InvalidCredentialsError(1003)
   ⚠️ 故意合并"用户不存在"和"密码错误"为同一错误，防止用户名枚举攻击
3. user.is_active == False → AccountDisabledError(1004)
4. 返回 (create_access_token(user_id), create_refresh_token(user_id))
```

### refresh

```
输入: refresh_token (str)
1. decode_token(token, expected_type="refresh") → 失败则 InvalidRefreshTokenError(1005)
2. 返回 create_access_token(user_id)
   注意: 不轮换 refresh_token（MVP 简化）
   TODO: 生产环境实现 refresh token rotation
```

---

## 六、API 端点

**Base prefix**: `/api/v1`

| # | 方法 | 路径 | HTTP 成功码 | 认证 |
|---|------|------|------------|------|
| 1 | POST | `/auth/register` | 201 | 否 |
| 2 | POST | `/auth/login` | 200 | 否 |
| 3 | POST | `/auth/refresh` | 200 | 否（用 refresh_token） |

所有响应使用 `ApiResponse[T]` 封装，`request_id` 从 `request.state.request_id` 读取（由中间件注入）。

---

## 七、中间件

### request_log.py（实现）

```python
# 记录格式:
# INFO [POST] /api/v1/auth/login → 200 (23ms) req=a1b2c3d4
class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.request_id = str(uuid4())[:8]
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            f"[{request.method}] {request.url.path} "
            f"→ {response.status_code} ({elapsed_ms:.0f}ms) "
            f"req={request.state.request_id}"
        )
        return response
```

### rate_limit.py（骨架）

```python
# TODO: 实现基于 Redis 的限流
# 规则（来自 API 契约 §1.8）:
#   认证接口: 5 次/分钟/IP
#   下单接口: 30 次/分钟/用户
#   查询接口: 120 次/分钟/用户
# 实现方案: Redis INCR + EXPIRE (sliding window 或 fixed window)
# 注意: 需处理 X-Forwarded-For 头以获取真实 IP（反向代理场景）
async def check_rate_limit(...):
    pass
```

---

## 八、get_current_user 依赖

```python
# dependencies.py
# 其他模块的 router 通过 Depends(get_current_user) 获取当前用户

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> UserModel:
    """
    1. decode_token(token, expected_type="access")  → 401 if invalid
    2. SELECT users WHERE id = sub                  → 401 if not found
    3. check is_active                              → 422 (1004) if disabled
    4. return UserModel
    """
```

---

## 九、main.py 改动

```python
# 新增:
from src.pm_gateway.middleware.request_log import RequestLogMiddleware
from src.pm_gateway.api.router import router as auth_router

app.add_middleware(RequestLogMiddleware)
app.include_router(auth_router, prefix="/api/v1")
```

---

## 十、测试范围

### 单元测试

| 文件 | 覆盖点 |
|------|--------|
| `tests/unit/test_gateway_jwt.py` | create/decode、过期检测、type 混用拒绝 |
| `tests/unit/test_gateway_password.py` | hash 不可逆、verify 正确/错误密码 |
| `tests/unit/test_gateway_schemas.py` | 密码强度校验、username 格式、邮箱格式 |

### 集成测试

| 场景 | 预期 |
|------|------|
| 注册成功 | 201，users 行 + accounts 行均存在 |
| 重复用户名 | 409，错误码 1001 |
| 重复邮箱 | 409，错误码 1002 |
| 登录成功 | 200，返回 access_token + refresh_token |
| 错误密码 | 401，错误码 1003 |
| 账户禁用 | 422，错误码 1004 |
| 用 access_token 访问受保护接口 | 200 |
| 用过期/无效 token 访问 | 401 |
| 用 refresh_token 刷新 | 200，返回新 access_token |
| 用 access_token 当 refresh_token | 401，错误码 1005 |

---

## 十一、偏差记录

| 偏差 | 原设计 | 实际决定 | 理由 |
|------|--------|---------|------|
| 无 logout 端点 | 未定义 | MVP 不实现 | 纯无状态 JWT 无法服务端吊销，TODO 注释标明 |
| refresh token 不轮换 | 未定义 | MVP 不实现 | 简化实现，TODO 注释标明生产升级路径 |
| rate_limit 不实现 | API 契约有限流规则 | 骨架 + TODO | 开发阶段限流干扰测试，后续单独实现 |
| 无独立 domain/models.py | 实施计划 v4.1 有此分层 | 直接 ORM→Service→Schema | MVP 无复杂 domain logic，避免过度分层 |

---

*设计版本: v1.0 | 日期: 2026-02-20 | 状态: 已确认*
