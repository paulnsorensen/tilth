@history
Feature: Language matching regression history
  Each scenario names the commit or pull request that introduced its regression family.

  Background:
    Given an example workspace

  @790bdbe @pr270
  Scenario: Decorated Python keeps canonical identity and semantic source
    Given the file "history/decorated.py" contains exactly
      """
      @logged
      def run():
          return 1
      """
    When I search for "run"
    Then the search resolves "run" in "history/decorated.py" at line 2
      """
      @logged
      def run():
          return 1
      """
    When I read "history/decorated.py#run"
    Then the read returns exactly
      """
      @logged
      def run():
          return 1
      """
    When I replace "run" with "execute" using the read tag
    Then the edit is applied
    And the file "history/decorated.py" contains exactly
      """
      @logged
      def execute():
          return 1
      """
    When I search for "execute"
    Then the search resolves "execute" in "history/decorated.py" at line 2
      """
      @logged
      def execute():
          return 1
      """
    When I search for "run"
    Then the search has no matches

  @790bdbe @pr270
  Scenario: Rust attributes keep canonical identity and semantic source
    Given the file "history/attributes.rs" contains exactly
      """
      #[inline]
      pub fn attributed() -> u8 {
          1
      }
      """
    When I search for "attributed"
    Then the search resolves "attributed" in "history/attributes.rs" at line 2
      """
      #[inline]
      pub fn attributed() -> u8 {
          1
      }
      """
    When I read "history/attributes.rs#attributed"
    Then the read returns exactly
      """
      #[inline]
      pub fn attributed() -> u8 {
          1
      }
      """

  @790bdbe @pr270
  Scenario: Annotated Java declarations keep attached syntax
    Given the file "history/Annotated.java" contains exactly
      """
      @Deprecated
      public class Annotated {
          public int value() { return 1; }
      }
      """
    When I search for "Annotated"
    Then the search resolves "Annotated" in "history/Annotated.java" at line 2
      """
      @Deprecated
      public class Annotated {
          public int value() { return 1; }
      }
      """

  @790bdbe @pr270
  Scenario: Deep Rust definitions are searchable and readable by symbol
    Given the file "history/deep.rs" contains exactly
      """
      mod outer {
          mod inner {
              fn target() {
                  let value = 1;
              }
          }
      }
      """
    When I search for "target"
    Then the search resolves "target" in "history/deep.rs" at line 3
      """
      |        fn target() {
      |            let value = 1;
      |        }
      """
    When I read "history/deep.rs#target"
    Then the read returns lines 3 through 5 exactly
      """
      |        fn target() {
      |            let value = 1;
      |        }
      """

  @790bdbe @pr270
  Scenario: Deep TypeScript methods are unique structural definitions
    Given the file "history/deep.ts" contains exactly
      """
      export class Alpha {
        dispatch(): number {
          return 1;
        }
      }
      """
    When I search for "dispatch"
    Then the search resolves "dispatch" in "history/deep.ts" at line 2
      """
      |  dispatch(): number {
      |    return 1;
      |  }
      """

  @2f7c448 @pr61
  Scenario: Same-name Rust methods stay ambiguous to search
    Given the file "history/alpha.rs" contains exactly
      """
      pub struct Alpha;
      impl Alpha {
          pub fn dispatch(&self) {}
      }
      """
    And the file "history/beta.rs" contains exactly
      """
      pub struct Beta;
      impl Beta {
          pub fn dispatch(&self) {}
      }
      """
    When I search for "dispatch"
    Then the search is ambiguous between "history/alpha.rs" and "history/beta.rs"
  @790bdbe @pr270
  Scenario: Nested wrapped classes keep their decorated spans
    Given the file "history/nested.py" contains exactly
      """
      class Outer:
          @logged
          class Inner:
              pass
      """
    When I search for "Inner"
    Then the search resolves "Inner" in "history/nested.py" at line 3
      """
      |    @logged
      |    class Inner:
      |        pass
      """
    Given the file "history/nested.ts" contains exactly
      """
      namespace Shell {
          export class Core {}
      }
      """
    When I search for "Core"
    Then the search resolves "Core" in "history/nested.ts" at line 2
      """
      |    export class Core {}
      """

  @de772cf @pr218
  Scenario: Grouped and multi-name Go declarations retain the query name
    Given the file "history/consts.go" contains exactly
      """
      package sample

      const (
          StatusActive = 1
          StatusInactive = 2
      )
      var CounterA, CounterB int
      """
    When I search for "StatusInactive"
    Then the search resolves "StatusInactive" in "history/consts.go" at line 3
      """
      const (
          StatusActive = 1
          StatusInactive = 2
      )
      """
    When I search for "CounterB"
    Then the search resolves "CounterB" in "history/consts.go" at line 7
      """
      var CounterA, CounterB int
      """

  @c8c7005
  Scenario: TypeScript and JavaScript constants are real declarations
    Given the file "history/constants.ts" contains exactly
      """
      export const EXPORTED_VALUE = 1;
      const bareValue = 2;
      """
    When I search for "EXPORTED_VALUE"
    Then the search resolves "EXPORTED_VALUE" in "history/constants.ts" at line 1
      """
      export const EXPORTED_VALUE = 1;
      """
    When I search for "bareValue"
    Then the search resolves "bareValue" in "history/constants.ts" at line 2
      """
      const bareValue = 2;
      """
    Given the file "history/constants.js" contains exactly
      """
      export const JS_EXPORTED = 3;
      const jsBare = 4;
      """
    When I search for "JS_EXPORTED"
    Then the search resolves "JS_EXPORTED" in "history/constants.js" at line 1
      """
      export const JS_EXPORTED = 3;
      """
    When I search for "jsBare"
    Then the search resolves "jsBare" in "history/constants.js" at line 2
      """
      const jsBare = 4;
      """

  @aaac6eb @pr102
  Scenario: Bash functions and declarations remain discoverable
    Given the file "history/functions.sh" contains exactly
      """
      export E_VAR=1
      declare -r D_VAR=2
      readonly R_VAR=3
      regular_name() {
        echo one
      }

      hyphen-name() {
        echo two
      }
      """
    When I search for "E_VAR"
    Then the search resolves "E_VAR" in "history/functions.sh" at line 1
      """
      export E_VAR=1
      """
    When I search for "D_VAR"
    Then the search resolves "D_VAR" in "history/functions.sh" at line 2
      """
      declare -r D_VAR=2
      """
    When I search for "R_VAR"
    Then the search resolves "R_VAR" in "history/functions.sh" at line 3
      """
      readonly R_VAR=3
      """
    When I search for "regular_name"
    Then the search resolves "regular_name" in "history/functions.sh" at line 4
      """
      regular_name() {
        echo one
      }
      """
    When I search for "hyphen-name"
    Then the content search finds 1 match in "history/functions.sh" at line 8
      """
      hyphen-name() {
      """

  @bc5e77f @pr15
  Scenario: A declaration wins over comments strings and call sites
    Given the file "history/decoys.rs" contains exactly
      """
      // real_target is documented here.
      const TEXT: &str = "real_target";
      fn caller() { real_target(); }

      fn real_target() -> u8 {
          1
      }
      """
    When I search for "real_target"
    Then the search resolves "real_target" in "history/decoys.rs" at line 5
      """
      fn real_target() -> u8 {
          1
      }
      """

  @abb9711 @7e120a5
  Scenario: Elixir declaration forms remain structural symbols
    Given the file "history/forms.ex" contains exactly
      """
      defmodule MyApp.Greeter do
        def hello(name) do
          name
        end
        defp private_helper(x), do: x + 1
        defmacro my_macro(expr) do
          expr
        end
        def safe_div(a, b) when b != 0 do
          a / b
        end
        defp checked(x) when is_integer(x), do: x
        defguard is_positive(x) when x > 0
        defdelegate count(list), to: Enum

        defmodule Inner do
          def nested_func, do: :ok
        end
      end
      """
    When I search for "hello"
    Then the search resolves "hello" in "history/forms.ex" at line 2
      """
      |  def hello(name) do
      |    name
      |  end
      """
    When I search for "private_helper"
    Then the search resolves "private_helper" in "history/forms.ex" at line 5
      """
      |  defp private_helper(x), do: x + 1
      """
    When I search for "my_macro"
    Then the search resolves "my_macro" in "history/forms.ex" at line 6
      """
      |  defmacro my_macro(expr) do
      |    expr
      |  end
      """
    When I search for "safe_div"
    Then the search resolves "safe_div" in "history/forms.ex" at line 9
      """
      |  def safe_div(a, b) when b != 0 do
      |    a / b
      |  end
      """
    When I search for "checked"
    Then the search resolves "checked" in "history/forms.ex" at line 12
      """
      |  defp checked(x) when is_integer(x), do: x
      """
    When I search for "is_positive"
    Then the search resolves "is_positive" in "history/forms.ex" at line 13
      """
      |  defguard is_positive(x) when x > 0
      """
    When I search for "count"
    Then the search resolves "count" in "history/forms.ex" at line 14
      """
      |  defdelegate count(list), to: Enum
      """
    When I search for "Inner"
    Then the search resolves "Inner" in "history/forms.ex" at line 16
      """
      |  defmodule Inner do
      |    def nested_func, do: :ok
      |  end
      """
