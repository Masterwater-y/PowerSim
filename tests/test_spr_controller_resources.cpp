#include "fastsim/spr_controller_resources.hpp"
#include <cassert>
int main() {
    fastsim::SprControllerResources services;
    services.configure({2,3,4});
    services.reserve(1,0,true,true,20,10,0);
    services.reserve(2,0,true,false,30,11,0);
    assert(services.admission(0,true,false,5)==20);
    assert(services.admission(0,false,true,5)==5);
    assert(services.admission(1,true,true,5)==5);
    assert(services.data_service(0,10,5)->ticket==1);
    services.finish(1,40);
    assert(services.data_service(0,10,5)->data_ready==20);
    assert(!services.data_service(0,10,25));
    services.finish(1,20);
    services.invalidate(10);
    assert(!services.data_service(0,10,5));
    assert(services.admission(0,true,false,5)==20);
    services.finish(1,40);
    assert(services.admission(0,true,false,5)==30);
    services.expire(30);
    assert(services.admission(0,true,false,30)==30);
    assert(services.next_release(30)==40);
    services.reserve(3,1,false,true,50,12,30,
        fastsim::SprControllerResources::Origin::kDemand,0,false);
    assert(!services.data_service(1,12,31));
    services.select_data(3,45);
    services.finish(3,60);
    assert(services.data_service(1,12,31)->data_ready==45);
    services.reserve(4,2,true,true,90,13,60,
        fastsim::SprControllerResources::Origin::kDemand,0,true,30);
    assert(services.data_service(2,13,31,false)->ticket==4);
    assert(!services.known_data_service(2,13,31));
}
