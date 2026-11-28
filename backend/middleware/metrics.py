import time
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        
        status_code = response.status_code
        endpoint = request.url.path

        log_message = "Request metrics"
        extra_info = {
            "endpoint": endpoint,
            "method": request.method,
            "status_code": status_code,
            "response_time_ms": f"{process_time * 1000:.2f}",
        }

        if status_code >= 400:
            logger.warning(log_message, extra={"extra_info": extra_info})
        else:
            logger.info(log_message, extra={"extra_info": extra_info})
            
        return response
