#!/usr/bin/env bash

fern generate --group ts-sdk --log-level debug --preview
cp -rf fern/.preview/fern-typescript-sdk/* skyvern-ts/client/