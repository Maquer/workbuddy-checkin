FROM python:3.12-slim

WORKDIR /app

COPY workbuddy_checkin.py .

RUN mkdir -p auths

# 默认每天 9 点签到
ENV CRON_HOUR=9

CMD ["python3", "workbuddy_checkin.py", "cron", "9"]
