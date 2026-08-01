//+------------------------------------------------------------------+
//|                                                 Zmq.mqh (stub)   |
//+------------------------------------------------------------------+
#ifndef ZMQ_MQH
#define ZMQ_MQH

#define ZMQ_PUB 1
#define ZMQ_SUB 2

class Context
{
public:
   Context() {}
};

class ZmqMsg
{
private:
   string m_data;

public:
   ZmqMsg() : m_data("") {}
   ZmqMsg(const string &data) : m_data(data) {}
   string getData() { return m_data; }
   void setData(const string &data) { m_data = data; }
};

class Socket
{
public:
   Socket(Context *ctx, int type) {}
   bool bind(const string address) { return true; }
   bool connect(const string address) { return true; }
   bool send(ZmqMsg &msg) { return true; }
   bool recv(ZmqMsg &msg, bool dontWait = false) { return false; }
   bool setSubscribe(const string filter) { return true; }
   bool setReceiveTimeout(int timeoutMs) { return true; }
};

#endif
