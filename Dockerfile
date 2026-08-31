# Streamda Proxy — HF Spaces Docker image
# Read: https://huggingface.co/docs/hub/spaces-sdks-docker
FROM python:3.12-slim

# Dev-mode support (HF requirement)
RUN useradd -m -u 1000 user
WORKDIR /app

COPY --chown=user ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

COPY --chown=user ./app /app

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
