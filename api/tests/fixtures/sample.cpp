// Sample C++ file for semantic chunking tests
// Tests: class_specifier, namespace_definition, function_definition

#include <iostream>
#include <string>

// Namespace definition
namespace geometry {

// Class definition
class Shape {
public:
    virtual double area() const = 0;
    virtual ~Shape() = default;
};

// Another class
class Rectangle : public Shape {
private:
    double width;
    double height;

public:
    Rectangle(double w, double h) : width(w), height(h) {}

    double area() const override {
        return width * height;
    }

    double perimeter() const {
        return 2 * (width + height);
    }
};

} // namespace geometry

// Struct (C++ style)
struct Point3D {
    double x, y, z;

    double magnitude() const {
        return std::sqrt(x*x + y*y + z*z);
    }
};

// Function definition
void print_area(const geometry::Shape& shape) {
    std::cout << "Area: " << shape.area() << std::endl;
}

// Main function
int main() {
    geometry::Rectangle rect(5.0, 3.0);
    print_area(rect);
    return 0;
}
