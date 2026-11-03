// Isolated executable: allocation injection must not alter the full test suite.
#include "fastsim/store_request_engine.hpp"
#include <cstdlib>
#include <iostream>
#include <new>

namespace { bool fail_next_allocation=false; }
void* operator new(std::size_t size) {
    if(fail_next_allocation) {fail_next_allocation=false;throw std::bad_alloc();}
    if(void* p=std::malloc(size ? size : 1))return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept {std::free(p);}
void operator delete(void* p,std::size_t) noexcept {std::free(p);}

int main() {
    fastsim::StoreRequestEngine engine({});
    const fastsim::PendingOwner owner{0,1,0,1};
    fail_next_allocation=true;
    bool failed=false;
    try {engine.allocate({owner,10});}
    catch(const std::bad_alloc&) {failed=true;}
    if(!failed || engine.sq_size()!=0 || engine.tracked_lines()!=0 ||
       engine.counters().sq_allocated!=0) {
        std::cerr<<"failed allocation left a ghost SQ or line\n";return 1;
    }
    if(!engine.allocate({owner,10}) || engine.sq_size()!=1 ||
       engine.tracked_lines()!=1 || engine.counters().sq_allocated!=1) {
        std::cerr<<"retry after allocation failure did not accept exactly once\n";return 1;
    }
    std::cout<<"store request allocation failure test passed\n";
}
