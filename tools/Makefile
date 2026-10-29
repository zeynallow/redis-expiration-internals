REDIS_SRC ?= /home/user/src/redis-8.10.3
HIREDIS = $(REDIS_SRC)/deps/hiredis

loadgen: loadgen.c
	$(CC) -O2 -Wall -o $@ $< -I$(HIREDIS) $(HIREDIS)/libhiredis.a -lpthread

clean:
	rm -f loadgen
