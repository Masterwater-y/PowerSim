#define _GNU_SOURCE
#include <errno.h>
#include <linux/perf_event.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Controlled units/request probes, not a claim of exact SPR implementation.
 * Working-set walks use a randomized dependent cache-line ring. Cache levels
 * are selected by capacity scans, not inferred solely from CLFLUSH. */
static const uint64_t codes[10][3] = {
    {0x81d0,0x82d0,0x01d1}, {0x81d0,0x08d1,0x40d1},
    {0xff24,0x2724,0x412e}, {0x08d1,0x2724,0x412e},
    {0x4023,0x4f2e,0x82d0}, {0x4023,0x0226,0x1f25},
    {0x81d0,0x82d0,0x01d1}, {0x08d1,0x10d1,0x20d1},
    {0x01d1,0x40d1,0x02d1}, {0x04d1,0x81d0,0x82d0}
};
static const char *fields[10][3] = {
    {"perf_retired_loads","perf_retired_stores","perf_l1d_load_hits"},
    {"perf_retired_loads","perf_l1d_load_misses","perf_l1d_load_fb_hits"},
    {"perf_l2_accesses","perf_l2_demand_misses","perf_llc_misses_core"},
    {"perf_l1d_load_misses","perf_l2_demand_misses","perf_llc_misses_core"},
    {"perf_l2_writebacks","perf_llc_accesses_core","perf_retired_stores"},
    {"perf_l2_writebacks","diagnostic_l2_lines_out_non_silent","diagnostic_l2_lines_in"},
    {"retired_loads","retired_stores","retired_l1_load_hits"},
    {"retired_l1_load_misses","retired_l2_load_misses","retired_l3_load_misses"},
    {"retired_l1_load_hits","retired_load_fb_hits","retired_l2_load_hits"},
    {"retired_l3_load_hits","retired_loads","retired_stores"}
};
static int number(const char *s,uint64_t *value) {
    if (!s[0] || s[0]=='-') return 0;
    char *end; errno=0; *value=strtoull(s,&end,10);
    return !errno && !*end;
}
static int valid_case(const char *s) {
    const char *names[]={"load","store","forward","fb","walk","walk_clean","store_walk","split_mixed","cmps",
                         "prefetch","cold_prefetch","locked_rmw","rep_movsb",
                         "prefetchnta","prefetcht1","prefetcht2","prefetchw","cold_locked_rmw"};
    for (unsigned i=0;i<sizeof(names)/sizeof(names[0]);++i)
        if(!strcmp(s,names[i])) return 1;
    return 0;
}
static uint64_t rng(uint64_t *state) {
    *state^=*state<<13; *state^=*state>>7; *state^=*state<<17;return *state;
}
__attribute__((noinline)) static void bench(const char *kind,unsigned n,unsigned char *a) {
    if (!strcmp(kind,"walk") || !strcmp(kind,"walk_clean") || !strcmp(kind,"store_walk")) {
        unsigned count=n;uintptr_t p=(uintptr_t)a;
        if(!strcmp(kind,"walk") || !strcmp(kind,"walk_clean"))
            __asm__ volatile("1: mov (%0),%0\n\tdec %1\n\tjnz 1b" : "+r"(p),"+r"(count) : : "cc","memory");
        else
            __asm__ volatile("1: movq $1,8(%0)\n\tmov (%0),%0\n\tdec %1\n\tjnz 1b" : "+r"(p),"+r"(count) : : "cc","memory");
        return;
    }
    unsigned char *b=a+256;
    if(!strcmp(kind,"rep_movsb")) {
        __asm__ volatile("mov %[n],%%r8d\n\t.p2align 6\n1:\n\t"
            "mov %[a],%%rsi\n\tmov %[b],%%rdi\n\tmov $8,%%ecx\n\trep movsb\n\t"
            "dec %%r8d\n\tjnz 1b\n\t"
            : : [n]"r"(n),[a]"r"(a),[b]"r"(b)
            : "rcx","rsi","rdi","r8","cc","memory");
        return;
    }
    if(!strcmp(kind,"split_mixed"))a+=60;
#define LOOP(body) __asm__ volatile("xor %%eax,%%eax\n\tmov %[n],%%ecx\n\t.p2align 6\n1:\n\t" body "dec %%ecx\n\tjnz 1b" : : [n]"r"(n),[a]"r"(a),[b]"r"(b) : "rax","rcx","rdx","rsi","rdi","cc","memory")
    if(!strcmp(kind,"load")) { LOOP("movq (%[a]),%%rax\n\t"); }
    else if(!strcmp(kind,"store")) { LOOP("movq %%rax,8(%[a])\n\t"); }
    else if(!strcmp(kind,"forward")) { LOOP("movq %%rax,8(%[a])\n\tmovq 8(%[a]),%%rdx\n\t"); }
    else if(!strcmp(kind,"fb")) { LOOP("clflush (%[a])\n\tmfence\n\tmovq (%[a]),%%rax\n\tmovq (%[a]),%%rdx\n\t"); }
    else if(!strcmp(kind,"split_mixed")) { LOOP("clflush 4(%[a])\n\tmfence\n\tmovq (%[a]),%%rax\n\t"); }
    else if(!strcmp(kind,"cmps")) { LOOP("mov %[a],%%rsi\n\tmov %[b],%%rdi\n\tcmpsq\n\t"); }
    else if(!strcmp(kind,"prefetch")) { LOOP("prefetcht0 (%[a])\n\t"); }
    else if(!strcmp(kind,"prefetchnta")) { LOOP("prefetchnta (%[a])\n\t"); }
    else if(!strcmp(kind,"prefetcht1")) { LOOP("prefetcht1 (%[a])\n\t"); }
    else if(!strcmp(kind,"prefetcht2")) { LOOP("prefetcht2 (%[a])\n\t"); }
    else if(!strcmp(kind,"prefetchw")) { LOOP("prefetchw (%[a])\n\t"); }
    else if(!strcmp(kind,"cold_prefetch")) { LOOP("clflush (%[a])\n\tmfence\n\tprefetcht0 (%[a])\n\tlfence\n\t"); }
    else if(!strcmp(kind,"locked_rmw")) { LOOP("lock incq 8(%[a])\n\t"); }
    else if(!strcmp(kind,"cold_locked_rmw")) { LOOP("clflush (%[a])\n\tmfence\n\tlock incq 8(%[a])\n\t"); }
#undef LOOP
}
int main(int argc,char **argv) {
    uint64_t n,size,group,cpu;
    if(argc!=6 || !valid_case(argv[1]) || !number(argv[2],&n) || !number(argv[3],&size) ||
       !number(argv[4],&group) || !number(argv[5],&cpu) || !n || n>1000000000 ||
       size<4096 || size>512ULL*1024*1024 || size%64 || group>=10 || cpu>=CPU_SETSIZE) return 2;
    cpu_set_t allowed,selected;
    if(sched_getaffinity(0,sizeof(allowed),&allowed) || !CPU_ISSET(cpu,&allowed))return 3;
    CPU_ZERO(&selected);CPU_SET(cpu,&selected);
    if(sched_setaffinity(0,sizeof(selected),&selected))return 3;
    unsigned char *data=NULL;const size_t lines=size/64;
    if(posix_memalign((void**)&data,64,size))return 8;
    size_t *order=malloc(lines*sizeof(*order));if(!order){free(data);return 8;}
    memset(data,0,size);for(size_t i=0;i<lines;++i)order[i]=i;
    uint64_t state=0x28d7cb857c;
    for(size_t i=lines-1;i;i--){size_t j=rng(&state)%(i+1),tmp=order[i];order[i]=order[j];order[j]=tmp;}
    for(size_t i=0;i<lines;++i)*(uintptr_t*)(data+order[i]*64)=(uintptr_t)(data+order[(i+1)%lines]*64);
    if(!strcmp(argv[1],"walk_clean")) {
        for(size_t i=0;i<lines;++i)__asm__ volatile("clflush (%0)" : : "r"(data+64*i) : "memory");
        __asm__ volatile("mfence" ::: "memory");
    }
    unsigned char *start=data+order[0]*64;
    /* All pages resident and pointer ring warmed before counting. */
    bench("walk",(unsigned)lines,start);
    __asm__ volatile("cld" ::: "cc");
    bench(argv[1],1024,(!strcmp(argv[1],"walk") || !strcmp(argv[1],"walk_clean") || !strcmp(argv[1],"store_walk"))?start:data);
    int fds[4];
    for(int i=0;i<4;++i){
        struct perf_event_attr a={0};a.size=sizeof(a);
        a.type=i==3?PERF_TYPE_HARDWARE:PERF_TYPE_RAW;
        a.config=i==3?PERF_COUNT_HW_INSTRUCTIONS:codes[group][i];
        a.disabled=i==0;a.pinned=i==0;a.exclude_kernel=1;a.exclude_hv=1;
        a.read_format=PERF_FORMAT_GROUP|PERF_FORMAT_TOTAL_TIME_ENABLED|PERF_FORMAT_TOTAL_TIME_RUNNING;
        fds[i]=syscall(__NR_perf_event_open,&a,0,-1,i?fds[0]:-1,0);
        if(fds[i]<0){perror("perf_event_open");return 4;}
    }
    if(ioctl(fds[0],PERF_EVENT_IOC_RESET,PERF_IOC_FLAG_GROUP) ||
       ioctl(fds[0],PERF_EVENT_IOC_ENABLE,PERF_IOC_FLAG_GROUP))return 5;
    bench(argv[1],(unsigned)n,(!strcmp(argv[1],"walk") || !strcmp(argv[1],"walk_clean") || !strcmp(argv[1],"store_walk"))?start:data);
    if(ioctl(fds[0],PERF_EVENT_IOC_DISABLE,PERF_IOC_FLAG_GROUP))return 5;
    struct{uint64_t nr,enabled,running,values[4];} r={0};
    if(read(fds[0],&r,sizeof(r))!=sizeof(r) || r.nr!=4)return 6;
    if(!r.running || r.running!=r.enabled)return 7;
    printf("{\"schema\":\"hardware-perf-cache-probe-v1\",\"case\":\"%s\",\"iterations\":%lu,\"working_set_bytes\":%lu,\"cpu\":%d,\"group\":%lu,\"time_enabled\":%lu,\"time_running\":%lu,\"counts\":{",argv[1],n,size,sched_getcpu(),group,r.enabled,r.running);
    for(int i=0;i<4;++i)printf("%s\"%s\":%lu",i?",":"",i==3?"instructions":fields[group][i],r.values[i]);
    puts("}}");
    for(int i=0;i<4;++i)close(fds[i]);
    free(order);free(data);return 0;
}
