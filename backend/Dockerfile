# The same Python as CI and local development — the lock (requirements.txt) is
# resolved for it. Only the runtime lock: the test/lint tools stay out of the image.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The release version, from the git tag (see app/version.py). Passed by the
# tag-triggered release workflow; an untagged build reports 0.0.0-dev.
ARG APP_VERSION=""
ARG GIT_COMMIT=""
ENV APP_VERSION=$APP_VERSION \
    GIT_COMMIT=$GIT_COMMIT

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
