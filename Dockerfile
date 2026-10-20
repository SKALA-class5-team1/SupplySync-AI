FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 의존성 먼저 설치 (코드만 바뀌면 이 층은 캐시를 재사용한다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 생존 검사: /health 가 200 이면 정상 (slim 이미지에는 curl 이 없어 파이썬으로 호출한다)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# 컨테이너 밖에서 접속하려면 0.0.0.0 으로 열어야 한다
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
