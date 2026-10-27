#!/bin/bash
cd "$(dirname "$0")"
npm install --omit=dev && node src/setup.js
read -p "Press Enter to close"
