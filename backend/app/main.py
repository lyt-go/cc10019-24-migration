"""用户标签画像与行为分析平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import settings
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="用户标签画像与行为分析平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UnderscoreModulePathMiddleware(BaseHTTPMiddleware):
    """兼容历史路径：前端仍用 /api/tag_compute，后端路由是 /api/tag-compute。

    只把第一段模块名里的下划线换成连字符，其余路径原样保留。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.scope["path"]
        if path.startswith("/api/"):
            head, _, tail = path[len("/api/"):].partition("/")
            normalized = head.replace("_", "-")
            if normalized != head:
                request.scope["path"] = f"/api/{normalized}" + (f"/{tail}" if tail else "")
        return await call_next(request)


app.add_middleware(UnderscoreModulePathMiddleware)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
