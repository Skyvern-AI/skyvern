#!/bin/bash

echo "Starting streaming..."

while true; do
    xwd -root | xwdtopnm | pnmtopng > /tmp/skyvern_screenshot.png
    sleep 1
done
