#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <string.h>
#include "gem5_roi_marker.h"
int main(void) {
 const size_t huge=2u<<20;
 void* raw=mmap(0,huge*3,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 if(raw==MAP_FAILED)return 2;
 unsigned char* p=(unsigned char*)(((uintptr_t)raw+huge-1)&~(uintptr_t)(huge-1));
 if(madvise(p,huge,MADV_HUGEPAGE))perror("madvise");
 memset(p,1,huge);
 printf("translation-probe region=%p\n",(void*)p);
 FILE* smaps=fopen("/proc/self/smaps","r");
 if(smaps){
  char line[512]; int region=0; unsigned long begin,end;
  while(fgets(line,sizeof(line),smaps)){
   if(sscanf(line,"%lx-%lx",&begin,&end)==2)
    region=(uintptr_t)p>=begin && (uintptr_t)p<end;
   if(region && (strstr(line,"AnonHugePages:") || strstr(line,"KernelPageSize:")))
    fputs(line,stdout);
  }
  fclose(smaps);
 }
 fflush(stdout);
 gem5_roi_checkpoint_once("dynamic-translation-probe-checkpoint",1);
 /* KVM may execute beyond the checkpoint until its synchronization quantum.
  * Sleep across that handoff so the O3 producer sees the pre-split mapping. */
 usleep(10000);
 gem5_roi_begin_once("dynamic-translation-probe-measurement",1);
 volatile unsigned char* vp=p;
 volatile unsigned long sum=vp[0];
 /* The UART ROI marker is asynchronous. Keep both geometries observable
  * after it reaches the producer; collect at least 500000 user records. */
 for(unsigned i=0;i<10000;++i)sum+=vp[(i*4096)&(huge-1)];
 if(mprotect(p+4096,4096,PROT_READ))return 3;
 sum+=vp[0];sum+=vp[4096];

 for(unsigned long i=0;i<100000000ul;++i)sum+=p[(i*4096)&(huge-1)];
 printf("probe=%lu\n",sum);
 return 0;
}
