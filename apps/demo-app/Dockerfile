FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir redis==5.2.1
COPY tinyprom.py app.py worker.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8001
CMD ["python", "app.py"]
