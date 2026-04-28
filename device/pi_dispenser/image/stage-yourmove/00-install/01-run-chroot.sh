#!/bin/bash
# Download and install go2rtc ARM binary
set -euo pipefail

GO2RTC_VERSION="1.9.4"
GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_arm"

curl -L -o /usr/local/bin/go2rtc "$GO2RTC_URL"
chmod +x /usr/local/bin/go2rtc
echo "go2rtc ${GO2RTC_VERSION} installed"
