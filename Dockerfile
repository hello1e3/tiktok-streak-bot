FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py sender.py ./

# На Bothost постоянные данные хранятся только в /app/data.
RUN mkdir -p /app/data && chmod 777 /app/data
CMD ["python", "-u", "bot.py"]
