FROM ubuntu:22.04

ARG LOCAL_DEV_UID
ARG LOCAL_DEV_SSH_PASSWORD

# Avoid interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    sudo \
    curl \
    software-properties-common \
    git \
    openssh-server \
    iptables \
    unzip \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    # playwright deps
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 

# SSH setup
RUN mkdir /var/run/sshd
RUN useradd -rm -d /home/skyvern -s /bin/bash -g root -G sudo -o -u ${LOCAL_DEV_UID:-1000} skyvern
RUN echo "skyvern:${LOCAL_DEV_SSH_PASSWORD}" | chpasswd

# Setup NVM and Node.js
ENV NVM_DIR /home/skyvern/.nvm
ENV NODE_VERSION 20.12.2

RUN mkdir -p $NVM_DIR && \
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \
    chown -R skyvern:root $NVM_DIR

# Configure bashrc
COPY ./.bashrc /home/skyvern/.bashrc  
RUN chown skyvern:root /home/skyvern/.bashrc

# Add skyvern to sudo group without password
RUN echo "skyvern ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/skyvern

# Install Node.js using NVM and set as default
USER skyvern
RUN . "$NVM_DIR/nvm.sh" && \
    nvm install $NODE_VERSION && \
    nvm alias default $NODE_VERSION && \
    nvm use default

# Setup poetry
RUN curl -sSL https://install.python-poetry.org | python3

# Back to Root
USER root

# Clean up apt
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Start the SSH server:
# -D: Run sshd in the foreground (not as a daemon)
# -e: Send log output to stderr instead of syslog
# This is useful for Docker containers as it allows you to view logs directly
# and keeps the SSH process as the main process, preventing the container from terminating
CMD ["/usr/sbin/sshd", "-D", "-e"]