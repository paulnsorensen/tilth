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
      FIXTURE = """
      {"prompt": "count ripgrep's lines"}
      """

      def caller():
          after_the_apostrophe()
      ```
    And the file "upstream/single.py" contains exactly
      """
      FIXTURE = '''
      owner's fixture
      '''

      def single_caller():
          after_the_apostrophe()
      """
    And the file "upstream/hash.py" contains exactly
      """
      # caller's note
      def hash_caller():
          after_the_apostrophe()
      """
    When I grok "after_the_apostrophe"
    Then grok resolves "after_the_apostrophe" in "upstream/target.py" at line 1 without ambiguity
      """
      def after_the_apostrophe():
          pass
      """
    And grok reports callers exactly
      """
      upstream/caller.py:6 caller
      upstream/hash.py:3 hash_caller
      upstream/single.py:6 single_caller
      """
    When I grok "after_the_apostrophe"
    Then grok reports callers exactly
      """
      upstream/caller.py:6 caller
      upstream/hash.py:3 hash_caller
      upstream/single.py:6 single_caller
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
    When I grok "Greeter"
    Then grok resolves "Greeter" in "upstream/greeter.rb" at line 1 without ambiguity
      """
      class Greeter
        def hello
          1
        end
      end
      """
    When I grok "Greeter::hello"
    Then grok resolves "hello" in "upstream/greeter.rb" at line 2 without ambiguity
      """
      |  def hello
      |    1
      |  end
      """
    When I grok "Factory::build"
    Then grok resolves "build" in "upstream/greeter.rb" at line 8 without ambiguity
      """
      |  def self.build
      |    Greeter.new
      |  end
      """

  Scenario: Same-line Ruby classes retain method ownership
    Given the file "upstream/same-line.rb" contains exactly
      """
      class SameLineRuby; def call; 7; end; end
      """
    When I grok "SameLineRuby::call"
    Then grok resolves "call" in "upstream/same-line.rb" at line 1 without ambiguity
      """
      class SameLineRuby; def call; 7; end; end
      """
    When I grok "OtherRuby::call"
    Then grok fails because "call" is not owned by "OtherRuby"

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
    When I grok "Widget"
    Then grok resolves "Widget" in "upstream/widget.cpp" at line 1 without ambiguity
      """
      class Widget {
      public:
          int inline_method() { return 2; }
          int outside_method();
          Widget operator+(const Widget& other) const { return other; }
      };
      """
    When I grok "Record"
    Then grok resolves "Record" in "upstream/widget.cpp" at line 8 without ambiguity
      """
      struct Record {
          int value;
      };
      """
    When I grok "Widget::inline_method"
    Then grok resolves "inline_method" in "upstream/widget.cpp" at line 3 without ambiguity
      """
      |    int inline_method() { return 2; }
      """
    When I grok "operator+"
    Then grok resolves "operator+" in "upstream/widget.cpp" at line 5 without ambiguity
      """
      |    Widget operator+(const Widget& other) const { return other; }
      """
    When I grok "Widget::operator+"
    Then grok resolves "operator+" in "upstream/widget.cpp" at line 5 without ambiguity
      """
      |    Widget operator+(const Widget& other) const { return other; }
      """
    When I grok "Widget::outside_method"
    Then grok resolves "outside_method" in "upstream/widget.cpp" at line 12 without ambiguity
      """
      int Widget::outside_method() { return 3; }
      """
    When I grok "Other::outside_method"
    Then grok resolves "outside_method" in "upstream/widget.cpp" at line 19 without ambiguity
      """
      int Other::outside_method() { return 4; }
      """
    When I grok "Nested::outside"
    Then grok resolves "outside" in "upstream/widget.cpp" at line 28 without ambiguity
      """
      |int Nested::outside() { return 5; }
      """
    When I grok "Nested::far"
    Then grok resolves "far" in "upstream/widget.cpp" at line 31 without ambiguity
      """
      int ns::Nested::far() { return 6; }
      """
    When I grok "Record::outside_method"
    Then grok fails because "outside_method" is not owned by "Record"

  Scenario: Same-line C++ classes retain method ownership
    Given the file "upstream/same-line.cpp" contains exactly
      """
      class SameLineCpp { public: int call() { return 7; } };
      """
    When I grok "SameLineCpp::call"
    Then grok resolves "call" in "upstream/same-line.cpp" at line 1 without ambiguity
      """
      class SameLineCpp { public: int call() { return 7; } };
      """
    When I grok "OtherCpp::call"
    Then grok fails because "call" is not owned by "OtherCpp"

  @issue225
  Scenario: TypeScript resolves missing JavaScript specifiers to source files
    Given the file "upstream/types.ts" contains exactly
      """
      export interface User { name: string; }
      export function makeUser(name: string): User { return { name }; }
      """
    And the file "upstream/consumer.ts" contains exactly
      """
      import { User, makeUser } from "./types.js";
      export function consume(user: User): string { return user.name; }
      export function create(name: string): User { return makeUser(name); }
      """
    When I inspect dependencies of "upstream/consumer.ts"
    Then the dependency report has exactly 1 local dependency "upstream/types.ts"
    When I inspect dependencies of "upstream/types.ts"
    Then the dependency report has exactly 1 caller dependent "upstream/consumer.ts" at line 3 owned by "create" calling "makeUser"
    Given the file "upstream/types.js" contains exactly
      """
      export class User { constructor(name) { this.name = name; } }
      export function makeUser(name) { return new User(name); }
      """
    When I inspect dependencies of "upstream/consumer.ts"
    Then the dependency report has exactly 1 local dependency "upstream/types.js"

    Given the file "upstream/view.tsx" contains exactly
      """
      export function View() { return <div />; }
      """
    And the file "upstream/view-consumer.ts" contains exactly
      """
      import { View } from "./view.jsx";
      export const view = View;
      """
    When I inspect dependencies of "upstream/view-consumer.ts"
    Then the dependency report has exactly 1 local dependency "upstream/view.tsx"
