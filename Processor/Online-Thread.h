#ifndef _Online_Thread
#define _Online_Thread

#include "Networking/Player.h"
#include "Math/gf2n.h"
#include "Math/Integer.h"
#include "Processor/Data_Files.h"

#include <vector>
using namespace std;

template<class sint, class sgf2n> class Machine;

template<class sint, class sgf2n>
class thread_info
{
  public: 

  int thread_num;
  Names*  Nms;
  typename sgf2n::mac_key_type *alpha2i;
  typename sint::mac_key_type *alphapi;

  Machine<sint, sgf2n>* machine;
  Processor<sint, sgf2n>* processor;

  static void* Main_Func(void *ptr);

  static void purge_preprocessing(Machine<sint, sgf2n>& machine);

  template<class T>
  static void print_usage(ostream& o, const vector<T>& regs, string name);

  void Sub_Main_Func();
};

#endif
