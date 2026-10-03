@upstream
Feature: Upstream language regressions stay fixed
  Background:
    Given an example workspace

  @issue228
  Scenario: Python callers survive apostrophes in triple strings and comments
    Given the file "upstream/target.py" contains exactly
      """
      def after_the_apostrophe():
          pass
      """
    And the file "upstream/caller.py" contains exactly
      ```
      from .target import after_the_apostrophe
      FIXTURE = """
      {"prompt": "count ripgrep's lines"}
      """

      def caller():
          after_the_apostrophe()
      ```
    And the file "upstream/single.py" contains exactly
      """
      from .target import after_the_apostrophe
      FIXTURE = '''
      owner's fixture
      '''

      def single_caller():
          after_the_apostrophe()
      """
    And the file "upstream/hash.py" contains exactly
      """
      from .target import after_the_apostrophe
      # caller's note
      def hash_caller():
          after_the_apostrophe()
      """
    When I search for "after_the_apostrophe"
    Then the search resolves "after_the_apostrophe" in "upstream/target.py" at line 1
      """
      def after_the_apostrophe():
          pass
      """
    And the search callers are exactly
      """
      upstream/caller.py:7 caller
      upstream/hash.py:4 hash_caller
      upstream/single.py:7 single_caller
      """
    When I search for "after_the_apostrophe"
    Then the search callers are exactly
      """
      upstream/caller.py:7 caller
      upstream/hash.py:4 hash_caller
      upstream/single.py:7 single_caller
      """

  @issue226
  Scenario: Ruby classes and singleton methods keep their owners
    Given the file "upstream/greeter.rb" contains exactly
      """
      class Greeter
        def hello
          1
        end
      end

      module Factory
        def self.build
          Greeter.new
        end
      end
      """
    When I search for "Greeter"
    Then the search resolves "Greeter" in "upstream/greeter.rb" at line 1
      """
      class Greeter
        def hello
          1
        end
      end
      """
    When I search for "hello"
    Then the search resolves "hello" in "upstream/greeter.rb" at line 2
      """
      |  def hello
      |    1
      |  end
      """
    When I search for "build"
    Then the search resolves "build" in "upstream/greeter.rb" at line 8
      """
      |  def self.build
      |    Greeter.new
      |  end
      """

  @issue227
  Scenario: C++ containers operators and qualified definitions keep canonical names
    Given the file "upstream/widget.cpp" contains exactly
      """
      class Widget {
      public:
          int inline_method() { return 2; }
          int outside_method();
          Widget operator+(const Widget& other) const { return other; }
      };

      struct Record {
          int value;
      };

      int Widget::outside_method() { return 3; }

      class Other {
      public:
          int outside_method();
      };

      int Other::outside_method() { return 4; }

      namespace ns {
      class Nested {
      public:
          int outside();
          int far();
      };

      int Nested::outside() { return 5; }
      }

      int ns::Nested::far() { return 6; }
      """
    When I search for "Widget"
    Then the search resolves "Widget" in "upstream/widget.cpp" at line 1
      """
      class Widget {
      public:
          int inline_method() { return 2; }
          int outside_method();
          Widget operator+(const Widget& other) const { return other; }
      };
      """
    When I search for "Record"
    Then the search resolves "Record" in "upstream/widget.cpp" at line 8
      """
      struct Record {
          int value;
      };
      """
    When I search for "inline_method"
    Then the search resolves "inline_method" in "upstream/widget.cpp" at line 3
      """
      |    int inline_method() { return 2; }
      """
    When I search for "outside_method"
    Then the search is ambiguous between "upstream/widget.cpp" lines 12 and 19
    When I search for "far"
    Then the search resolves "far" in "upstream/widget.cpp" at line 31
      """
      int ns::Nested::far() { return 6; }
      """
