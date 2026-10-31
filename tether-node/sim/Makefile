# Host (Linux) build of the portable core: unit tests and the simulator. Not used for the ESP32 image.
CORE := ../components/tether_core
SRCS := $(wildcard $(CORE)/src/*.c)
CFLAGS ?= -std=gnu11 -g -O1 -Wall -Wextra -Wno-unused-parameter -I$(CORE)/include
SAN ?= -fsanitize=address,undefined

all: build/tether_sim build/test_core

build/tether_sim: sim_main.c $(SRCS) $(wildcard $(CORE)/include/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) $(SAN) -o $@ sim_main.c $(SRCS)

build/test_core: ../tests/test_core.c $(SRCS) $(wildcard $(CORE)/include/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) $(SAN) -o $@ ../tests/test_core.c $(SRCS)

test: build/test_core
	build/test_core ../../testvectors/frames.json

clean:
	rm -rf build
.PHONY: all test clean
