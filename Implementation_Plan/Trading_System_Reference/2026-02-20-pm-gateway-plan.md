# pm_gateway 认证模块实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现 pm_gateway 模块，包含用户注册/登录/Token 刷新三个接口、JWT 双 Token 认证、请求日志中间件，以及供其他模块使用的 `get_current_user` 依赖注入。

**Architecture:** 纯无状态 HS256 JWT（access 30min + refresh 7d），注册时单事务同时写 `users` + `accounts` 表，限流中间件仅创建骨架，请求日志中间件实际实现。

**Tech Stack:** `python-jose[cryptography]`、`bcrypt>=4.0`（非 passlib）、FastAPI `OAuth2PasswordBearer`、SQLAlchemy async ORM、Pydantic v2

**设计文档:** `Planning/Implementation/2026-02-20-pm-gateway-design.md`

**对齐 API 契约:** `Planning/Detail_Design/02_API接口契约.md` §2.1–2.3

**状态: ✅ 全部完成 (2026-02-20)**

---

## 完成验收标准

```
✅ POST /api/v1/auth/register  → 201，同时创建 users + accounts 行
✅ POST /api/v1/auth/login     → 200，返回 access_token + refresh_token
✅ POST /api/v1/auth/refresh   → 200，返回新 access_token
✅ Refresh token 当 access token 用 → 401 (1005)
✅ get_current_user Depends() 可被其他模块 import 使用
✅ 所有请求在控制台打印日志 [METHOD] /path → status (Xms)
✅ make test 全绿 (85 单元 + 11 集成通过, 1 skipped)
✅ make lint 零报错
✅ make typecheck 零报错
```

---

## 已实现文件

| 文件 | 说明 |
|------|------|
| `src/pm_gateway/auth/password.py` | bcrypt hash/verify（直接用 bcrypt，不用 passlib） |
| `src/pm_gateway/auth/jwt_handler.py` | HS256 JWT 签发/验证，_raise_auth_error -> NoReturn |
| `src/pm_gateway/auth/dependencies.py` | get_current_user FastAPI Depends() |
| `src/pm_gateway/user/schemas.py` | Pydantic v2 请求/响应 schema，密码复杂度校验 |
| `src/pm_gateway/user/db_models.py` | UserModel ORM，映射现有 users 表 |
| `src/pm_gateway/user/service.py` | UserService(register/login/refresh)，login 返回 (UserModel, access, refresh) |
| `src/pm_gateway/middleware/request_log.py` | RequestLogMiddleware，注入 request_id |
| `src/pm_gateway/middleware/rate_limit.py` | TODO 骨架 |
| `src/pm_gateway/api/router.py` | 3 个端点 /register /login /refresh |
| `src/main.py` | 新增 add_middleware(RequestLogMiddleware) + include_router(auth_router) |
| `tests/unit/test_gateway_*.py` | 30 个单元测试 |
| `tests/integration/test_auth_flow.py` | 12 个集成测试（11 pass, 1 skip） |
| `tests/integration/conftest.py` | session-scoped client fixture（避免 asyncpg pool 问题） |

---

## 偏差记录

| 偏差 | 说明 |
|------|------|
| passlib → bcrypt 直接依赖 | passlib 已停止维护，与 bcrypt>=4.x 不兼容；改用 bcrypt>=4.0 |
| login 返回值 | 原计划 (str,str)；改为 (UserModel, str, str)，消除 router 中的二次 SELECT |
| HTTPException 用于 token 验证 | get_current_user 用 FastAPI HTTPException(401) + WWW-Authenticate header |
| _raise_auth_error → NoReturn | mypy strict 要求 |
| decode_token 返回类型 | dict[str, Any]（JWT payload 含 int exp/iat） |
| pydantic[email] 依赖 | EmailStr 需要 email-validator，pyproject.toml 更新 |
| integration conftest session-scoped | asyncpg pool 要求 session loop scope |

---

*计划版本: v1.0 | 日期: 2026-02-20 | 状态: ✅ 全部完成*
