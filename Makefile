# `make` builds the engine for this machine; OpenBench and similar tools call
# `make EXE=<path>`. The network is compiled in (engine/nets/default.nnue).
EXE ?= arhanpassant

.PHONY: all clean
all:
	cargo rustc --release -p arhanpassant --bin arhanpassant -- -C target-cpu=native --emit link=$(EXE)

clean:
	cargo clean
