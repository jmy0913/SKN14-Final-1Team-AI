# Contributing Guidelines

이 프로젝트의 실행을 위한 가이드라인 입니다.

# 실행방법

가상환경 설정하기

```python
conda create -n codenova-ai python=3.11 -y
conda activate codenova-ai
pip install -r requirements.txt
```

`.env` 설정하기

```
VLLM_API_URL=https://your_url-8000.proxy.runpod.net/v1
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_API_KEY=sk-xxxx
```

# 로컬 실행 방법

```bash
uvicorn main:app --reload --port 8001
```

# 도커 실행 방법
```
docker build -t codenova-ai .
docker run -d --name codenova-ai --env-file .env -p 8001:8001 codenova-ai
```

# API 호출방법

```sh
# get
curl -X GET "http://127.0.0.1:8001/"
```