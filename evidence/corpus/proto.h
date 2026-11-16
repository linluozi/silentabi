typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;

struct message_header {
    uint32_t magic;
    uint16_t version;
    uint16_t flags;
    uint32_t length;
    uint64_t timestamp;
};

struct payload_entry {
    uint32_t key;
    uint16_t type;
    uint16_t len;
    uint8_t data[16];
};

struct batch_message {
    struct message_header header;
    uint32_t count;
    struct payload_entry entries[8];
    uint32_t crc;
};

union scalar_value {
    int32_t i;
    double d;
    uint8_t bytes[8];
};

struct tagged_value {
    uint8_t tag;
    union scalar_value value;
    uint16_t quality;
};
