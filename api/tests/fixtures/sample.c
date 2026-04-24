// Sample C file for semantic chunking tests
// Tests: function_definition, struct_specifier, enum_specifier, typedef_declaration

#include <stdio.h>

// Struct definition
struct Point {
    int x;
    int y;
};

// Enum definition
enum Color {
    RED,
    GREEN,
    BLUE
};

// Typedef
typedef unsigned int uint32;

// Function definition
void print_point(struct Point p) {
    printf("Point: (%d, %d)\n", p.x, p.y);
}

// Another function
int add(int a, int b) {
    return a + b;
}

// Main function
int main() {
    struct Point p = {10, 20};
    print_point(p);
    return 0;
}
