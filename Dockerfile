FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.py recording_worker.py scheduler.py state.py ./
COPY templates templates
COPY static static
VOLUME /downloads
EXPOSE 8080
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["python", "/app/app.py"]
