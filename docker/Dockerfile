# wpe-studio in a container.
#
# The container renders on the HOST's X display through the mounted socket and
# the host's GPU through the nvidia runtime -- it is not a sandbox, it is a
# packaging choice, so the UI can be brought up on any box that has the same
# mounts without installing anything into the system.
#
# What it deliberately cannot do: the host-only bits (Steam's launch option,
# installing xsecurelock, the Cinnamon desktop-icons toggle, the autostart
# entry). Those need to touch the real session, so the app detects
# WPE_IN_DOCKER and greys them out instead of failing halfway through.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi \
      x11-utils x11-xserver-utils wmctrl xdotool xauth \
      libgl1 libglx-mesa0 libegl1 libx11-6 libxrandr2 libxinerama1 libxext6 \
      libsdl2-2.0-0 libfreeimage3 libmpv2 libfontconfig1 \
      libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
      libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
      libgbm1 libasound2t64 libpango-1.0-0 libcairo2 libatspi2.0-0t64 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV WPE_IN_DOCKER=1 \
    WPE_BIND=0.0.0.0 \
    WPE_PORT=8014 \
    PYTHONUNBUFFERED=1

WORKDIR /app
EXPOSE 8014
CMD ["python3", "/app/bin/wpe-studio", "--server"]
