FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=$ANDROID_SDK_ROOT
ENV PATH=$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH

RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools && \
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O /tmp/cmd-tools.zip && \
    unzip -q /tmp/cmd-tools.zip -d /tmp/cmd-tools && \
    mv /tmp/cmd-tools/cmdline-tools $ANDROID_SDK_ROOT/cmdline-tools/latest && \
    rm -rf /tmp/cmd-tools.zip /tmp/cmd-tools && \
    yes | sdkmanager --licenses >/dev/null 2>&1 || true && \
    sdkmanager "platforms;android-34" "build-tools;34.0.0" --sdk_root=$ANDROID_SDK_ROOT >/dev/null 2>&1

WORKDIR /app

COPY pages/ ./pages/
COPY app.py ./
RUN mkdir -p work uploads outputs

RUN pip install flask --no-cache-dir --quiet

ENV PORT=7860
EXPOSE $PORT

CMD ["python3", "app.py"]
