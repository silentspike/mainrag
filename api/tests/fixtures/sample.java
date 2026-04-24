// Sample Java file for semantic chunking tests
// Tests: class_declaration, interface_declaration, method_declaration, enum_declaration

package com.example.test;

// Interface definition
interface Drawable {
    void draw();
    default void clear() {
        System.out.println("Clearing...");
    }
}

// Enum definition
enum Status {
    PENDING,
    ACTIVE,
    COMPLETED,
    FAILED
}

// Class definition
public class Sample implements Drawable {
    private String name;
    private int value;
    private Status status;

    // Constructor
    public Sample(String name, int value) {
        this.name = name;
        this.value = value;
        this.status = Status.PENDING;
    }

    // Method implementation
    @Override
    public void draw() {
        System.out.println("Drawing: " + name);
    }

    // Getter method
    public String getName() {
        return name;
    }

    // Setter method
    public void setValue(int value) {
        this.value = value;
    }

    // Static method
    public static int calculate(int a, int b) {
        return a + b;
    }

    // Main method
    public static void main(String[] args) {
        Sample sample = new Sample("Test", 42);
        sample.draw();
    }
}
