import httpx

# 비동기 클라이언트
async_client = httpx.AsyncClient(
    timeout=10.0,
    follow_redirects=True
)

# 동기 클라이언트
sync_client = httpx.Client(
    timeout=10.0,
    follow_redirects=True
)

