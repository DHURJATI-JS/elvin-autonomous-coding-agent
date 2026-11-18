#!/bin/bash
# Double-click me in Finder.
cd "$(dirname "$0")" && bash "install/stop.sh"
echo
read -r -p "Press Enter to close this window..." _
