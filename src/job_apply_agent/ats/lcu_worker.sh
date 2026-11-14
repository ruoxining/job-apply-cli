#!/bin/sh
set -eu
/usr/libexec/at-spi-bus-launcher --launch-immediately --a11y=1 --screen-reader=1 &
sleep 1
AT_SPI_BUS_ADDRESS=$(gdbus call --session --dest org.a11y.Bus \
  --object-path /org/a11y/bus --method org.a11y.Bus.GetAddress | cut -d "'" -f 2)
test -n "$AT_SPI_BUS_ADDRESS"
export AT_SPI_BUS_ADDRESS
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec python -m dramatiq job_apply_agent.tasks --processes 1 --threads 1
