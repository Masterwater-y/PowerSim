CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static
LDFLAGS ?= -static

TARGET := analytics_st
SRC    := analytics_st.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	./$(TARGET) 2

clean:
	rm -f $(TARGET)
