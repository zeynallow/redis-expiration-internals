/* loadgen: low-overhead open-loop Redis load generator for latency measurement.
 *
 * Each thread owns one blocking hiredis connection and issues requests on a
 * fixed schedule (rate per thread). Latency is measured from the *scheduled*
 * send time (corrects coordinated omission) and also as pure service time
 * (actual send -> reply). Every request is recorded; raw samples are written
 * as CSV: sched_us(epoch),lat_us,svc_us,op
 *
 * usage: loadgen -p port -t threads -r rate_per_thread -d seconds -o out.csv
 *                [-n read_keyspace] [-k read_prefix] [-w write_fraction]
 *                [-x write_ttl_ms] [-v value_size]
 */
#include <hiredis.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static const char *host = "127.0.0.1";
static int port = 6399, nthreads = 2, duration = 10, vsize = 64;
static double rate = 5000, wfrac = 0.0;
static long keyspace = 1000000, wttl = 0;
static const char *prefix = "k:";
static const char *out = "loadgen.csv";

typedef struct { int64_t sched, lat, svc; int op; } sample_t;
typedef struct { int id; sample_t *s; long n, cap; } targ_t;

static int64_t now_us(int clk) {
    struct timespec ts; clock_gettime(clk, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static void *worker(void *p) {
    targ_t *a = p;
    redisContext *c = redisConnect(host, port);
    if (!c || c->err) { fprintf(stderr, "connect failed\n"); exit(1); }
    char *val = malloc(vsize + 1); memset(val, 'v', vsize); val[vsize] = 0;
    unsigned int seed = 1234567u * (a->id + 1);
    int64_t mono0 = now_us(CLOCK_MONOTONIC), wall0 = now_us(CLOCK_REALTIME);
    int64_t interval_ns = (int64_t)(1e9 / rate);
    int64_t end = mono0 + (int64_t)duration * 1000000;
    long i = 0;
    char key[64];
    for (;; i++) {
        int64_t sched = mono0 + (i * interval_ns) / 1000;
        if (sched >= end) break;
        int64_t t = now_us(CLOCK_MONOTONIC);
        if (t < sched) {
            struct timespec ts = { (sched - t) / 1000000, ((sched - t) % 1000000) * 1000 };
            nanosleep(&ts, NULL);
        }
        int op = ((double)rand_r(&seed) / RAND_MAX) < wfrac;
        int64_t send = now_us(CLOCK_MONOTONIC);
        redisReply *r;
        if (op) {
            snprintf(key, sizeof key, "w:%d:%ld", a->id, i);
            if (wttl) r = redisCommand(c, "SET %s %s PX %ld", key, val, wttl);
            else r = redisCommand(c, "SET %s %s", key, val);
        } else {
            snprintf(key, sizeof key, "%s%ld", prefix, (long)(rand_r(&seed) % keyspace));
            r = redisCommand(c, "GET %s", key);
        }
        int64_t done = now_us(CLOCK_MONOTONIC);
        if (!r) { fprintf(stderr, "conn error\n"); exit(1); }
        freeReplyObject(r);
        if (a->n < a->cap) {
            sample_t *s = &a->s[a->n++];
            s->sched = wall0 + (sched - mono0); s->lat = done - sched; s->svc = done - send; s->op = op;
        }
    }
    redisFree(c); free(val);
    return NULL;
}

int main(int argc, char **argv) {
    int o;
    while ((o = getopt(argc, argv, "h:p:t:r:d:o:n:k:w:x:v:")) != -1) {
        switch (o) {
        case 'h': host = optarg; break;
        case 'p': port = atoi(optarg); break;
        case 't': nthreads = atoi(optarg); break;
        case 'r': rate = atof(optarg); break;
        case 'd': duration = atoi(optarg); break;
        case 'o': out = optarg; break;
        case 'n': keyspace = atol(optarg); break;
        case 'k': prefix = optarg; break;
        case 'w': wfrac = atof(optarg); break;
        case 'x': wttl = atol(optarg); break;
        case 'v': vsize = atoi(optarg); break;
        default: fprintf(stderr, "bad option\n"); return 1;
        }
    }
    pthread_t th[64]; targ_t args[64];
    for (int i = 0; i < nthreads; i++) {
        args[i].id = i; args[i].n = 0; args[i].cap = (long)(rate * duration) + 16;
        args[i].s = malloc(sizeof(sample_t) * args[i].cap);
        pthread_create(&th[i], NULL, worker, &args[i]);
    }
    for (int i = 0; i < nthreads; i++) pthread_join(th[i], NULL);
    FILE *f = fopen(out, "w");
    fprintf(f, "sched_us,lat_us,svc_us,op\n");
    for (int i = 0; i < nthreads; i++)
        for (long j = 0; j < args[i].n; j++)
            fprintf(f, "%lld,%lld,%lld,%d\n", (long long)args[i].s[j].sched,
                    (long long)args[i].s[j].lat, (long long)args[i].s[j].svc, args[i].s[j].op);
    fclose(f);
    return 0;
}
