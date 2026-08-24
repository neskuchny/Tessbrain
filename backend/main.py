"""
TESSENT BRAIN - Application Entry Point
Точка входа для запуска API сервера
"""
import uvicorn

from backend.config import settings


def main():
    """Запуск API сервера"""
    uvicorn.run(
        "backend.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        workers=1 if settings.debug else settings.api_workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()



