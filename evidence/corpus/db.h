typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;

struct page_header {
    uint16_t page_type;
    uint16_t flags;
    uint32_t page_id;
    uint32_t checksum;
    uint64_t lsn;
};

struct slot {
    uint16_t offset;
    uint16_t length;
    uint8_t state;
};

struct record_header {
    uint16_t key_len;
    uint16_t val_len;
    uint8_t flags;
    uint32_t txn;
};

struct heap_page {
    struct page_header header;
    uint16_t slot_count;
    struct slot slots[16];
    uint8_t data[256];
};
