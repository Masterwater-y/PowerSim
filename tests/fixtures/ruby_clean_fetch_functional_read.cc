// Test-only controller and memory boundary for actual RubySystem methods.
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#define DPRINTF(...) do {} while (0)
#define warn(...) do {} while (0)
#define panic(...) std::abort()
using Addr = uint64_t;
// GENERATED_PERMISSION_ENUM
// GENERATED_STATE_ENUM
// GENERATED_STATE_PERMISSIONS
struct Request { int requestorId() const { return 0; } };
struct Packet {
    Request request;
    Request* req = &request;
    int requestorId() const { return request.requestorId(); }
    int data = 0;
    Addr getAddr() const { return 0x81f48ce58ULL; }
};
using PacketPtr = Packet*;
Addr makeLineAddress(Addr address, int bits) { return address & ~((Addr(1)<<bits)-1); }
struct Sequencer {
    int functionalWrite(PacketPtr) { return 0; }
};
struct AbstractController {
    bool directory = false;
    Directory_State state = Directory_State_I;
    AccessPermission permission = AccessPermission_NotPresent;
    int memory = 17, cached = 91, buffered = 73, reads = 0, writes = 0;
    bool payload = false;
    AccessPermission getAccessPermission(Addr) {
        return directory ? Directory_State_to_permission(state) : permission;
    }
    int functionalReadPriority() { return 30; }
    void functionalRead(Addr, PacketPtr pkt) {
        ++reads; pkt->data = directory ? memory : cached;
    }
    bool functionalReadBuffers(PacketPtr pkt) {
        if (payload) pkt->data = buffered;
        return payload;
    }
    int functionalWriteBuffers(PacketPtr pkt) {
        if (payload) buffered = pkt->data;
        return payload;
    }
    int functionalWrite(Addr, PacketPtr pkt) {
        ++writes; if (directory) memory = pkt->data; else cached = pkt->data;
        return 1;
    }
    Sequencer* getCPUSequencer() { return nullptr; }
    Sequencer* getDMASequencer() { return nullptr; }
};
struct Network {
    bool payload = false;
    int data = 74;
    bool functionalRead(PacketPtr pkt) {
        if (payload) pkt->data = data;
        return payload;
    }
    int functionalWrite(PacketPtr pkt) {
        if (payload) data = pkt->data;
        return payload;
    }
};
struct RubySystem {
    int m_block_size_bits = 6;
    std::map<int,int> requestorToNetwork{{0,0}};
    std::map<int,std::vector<AbstractController*>> netCntrls;
    std::vector<Network*> m_networks;
    bool simpleFunctionalRead(PacketPtr pkt);
    bool functionalWrite(PacketPtr pkt);
};
// PRODUCTION_FUNCTIONAL_READ
// PRODUCTION_FUNCTIONAL_WRITE
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
int main(int argc, char** argv) try {
    require(argc == 2, "one case name required");
    std::string mode = argv[1];
    AbstractController dir, cache;
    dir.directory = true;
    Network network;
    RubySystem ruby;
    ruby.netCntrls[0] = {&dir, &cache};
    ruby.m_networks = {&network};
    Packet pkt;
    if (mode == "clean-im") {
        dir.state = Directory_State_IM;
        cache.permission = AccessPermission_Busy;
        require(ruby.simpleFunctionalRead(&pkt), "clean fetch in IM lost authoritative memory");
        require(pkt.data == 17 && dir.reads == 1, "IM returned incorrect memory bytes");
    } else if (mode == "idle") {
        require(ruby.simpleFunctionalRead(&pkt) && pkt.data == 17, "idle memory unreadable");
    } else if (mode == "owner") {
        for (auto state : {Directory_State_M, Directory_State_IM}) {
            dir.state = state;
            cache.permission = AccessPermission_Read_Write;
            require(ruby.simpleFunctionalRead(&pkt) && pkt.data == 91, "modified owner lost priority");
            require(dir.reads == 0, "stale RAM used instead of modified owner");
        }
    } else if (mode == "writeback" || mode == "network") {
        dir.state = Directory_State_MI;
        cache.permission = AccessPermission_Busy;
        dir.payload = mode == "writeback";
        network.payload = mode == "network";
        require(ruby.simpleFunctionalRead(&pkt), "pending dirty bytes were lost");
        require(pkt.data == (dir.payload ? 73 : 74) && dir.reads == 0,
                "dirty data replaced with stale RAM");
    } else if (mode == "busy" || mode == "permissions") {
        const Directory_State busy[] = {Directory_State_MI, Directory_State_ID,
            Directory_State_ID_W, Directory_State_M_DRD, Directory_State_M_DRDI,
            Directory_State_M_DWR, Directory_State_M_DWRI};
        for (auto state : busy) {
            dir.state = state;
            require(Directory_State_to_permission(state) == AccessPermission_Busy,
                    "unrelated dirty/DMA transient became readable");
            require(!ruby.simpleFunctionalRead(&pkt) && dir.reads == 0,
                    "dirty/DMA transient fell back to stale memory");
        }
        require(Directory_State_to_permission(Directory_State_I) == AccessPermission_Read_Write,
                "idle directory permission changed");
        require(Directory_State_to_permission(Directory_State_M) == AccessPermission_Maybe_Stale,
                "modified directory permission changed");
    } else if (mode == "missing") {
        dir.directory = false;
        require(!ruby.simpleFunctionalRead(&pkt) && dir.reads == 0,
                "a truly missing line was accepted");
    } else if (mode == "write") {
        for (auto state : {Directory_State_I, Directory_State_IM}) {
            dir.state = state; dir.payload = true;
            cache.permission = AccessPermission_Busy; cache.payload = true;
            network.payload = true; pkt.data = 29;
            require(ruby.functionalWrite(&pkt), "functional write failed");
            require(dir.memory == 29 && dir.buffered == 29 && cache.cached == 29 &&
                    cache.buffered == 29 && network.data == 29,
                    "functional write missed a pending copy");
        }
    } else {
        throw std::runtime_error("unknown test mode");
    }
    return 0;
} catch (const std::exception& e) {
    std::cerr << e.what() << '\n'; return 1;
}
