FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8443 8444
CMD ["python", "bob.py", "--config", "/run/qkd/config.json"]
