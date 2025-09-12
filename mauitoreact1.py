import xml.etree.ElementTree as ET
import re
import os

# --- Mappings (Highly Simplified & Incomplete) ---
MAUI_TO_RN_ELEMENT_MAP = {
    "Label": "Text",
    "Button": "Button",
    "Entry": "TextInput",
    "StackLayout": "View",
    "Grid": "View",  # Grid layout is complex, View is a placeholder
    "ScrollView": "ScrollView",
    "Image": "Image",
    "ContentPage": "View", # Or SafeAreaView, or a screen wrapper
    "ContentView": "View",
    # Add more basic mappings
}

MAUI_TO_RN_PROP_MAP = {
    "Text": "children",  # For Label/Text
    "Text_Button": "title", # For Button
    "Placeholder": "placeholder",
    "Source": "source", # For Image, needs special handling for require()
    "Clicked": "onPress",
    "TextChanged": "onChangeText",
    "BackgroundColor": "backgroundColor",
    "TextColor": "color",
    "FontSize": "fontSize",
    "Padding": "padding", # Value needs conversion (e.g. "20" -> 20, "10,5" -> {paddingVertical:10, paddingHorizontal:5})
    "Margin": "margin",   # Similar value conversion needed
    "Orientation": "flexDirection", # For StackLayout
    "HorizontalOptions": "alignSelf", # Very rough mapping (e.g. "Center" -> "center")
    "VerticalOptions": "alignSelf",   # Very rough mapping
    "HeightRequest": "height",
    "WidthRequest": "width",
    "Spacing": "gap", # For StackLayout with flexDirection
    # Add more, many will need value transformations
}

MAUI_VALUE_CONVERSIONS = {
    "Orientation": {
        "Vertical": "column",
        "Horizontal": "row",
    },
    "HorizontalOptions": {
        "Start": "flex-start",
        "Center": "center",
        "End": "flex-end",
        "Fill": "stretch",
    },
    "VerticalOptions": { # Similar to HorizontalOptions for alignSelf
        "Start": "flex-start",
        "Center": "center",
        "End": "flex-end",
        "Fill": "stretch",
    }
    # Add more for colors, fonts, thickness, etc.
}

def sanitize_component_name(filename):
    base = os.path.basename(filename)
    name = os.path.splitext(os.path.splitext(base)[0])[0] # Remove .xaml.cs or .cs
    name = re.sub(r'[^a-zA-Z0-9_]', '', name)
    return name.capitalize() + "Screen" if name else "UnnamedScreen"

def convert_prop_name(maui_element_tag, maui_prop_key):
    if maui_element_tag == "Button" and maui_prop_key == "Text":
        return MAUI_TO_RN_PROP_MAP.get("Text_Button", maui_prop_key)
    return MAUI_TO_RN_PROP_MAP.get(maui_prop_key, maui_prop_key)

def convert_prop_value(maui_prop_key, maui_prop_value, rn_prop_key):
    if maui_prop_key in MAUI_VALUE_CONVERSIONS:
        return MAUI_VALUE_CONVERSIONS[maui_prop_key].get(maui_prop_value, f"'{maui_prop_value}' /* TODO: Check value */")

    if rn_prop_key in ["fontSize", "height", "width", "padding", "margin", "gap"]:
        try:
            return int(maui_prop_value) # Attempt to convert to number
        except ValueError:
            # TODO: Handle thickness "10,20" or named sizes
            return f"'{maui_prop_value}' /* TODO: Check numeric/string value */"
    if rn_prop_key == "source" and isinstance(maui_prop_value, str):
         # Extremely naive: assumes a remote URI or a placeholder for require
        if maui_prop_value.startswith("http"):
            return f"{{ uri: '{maui_prop_value}' }}"
        else:
            return f"require('./{maui_prop_value}') /* TODO: Verify path */"

    # Default: treat as string, unless it's a binding expression (which we ignore for now)
    if isinstance(maui_prop_value, str) and not maui_prop_value.startswith("{"):
        return f"'{maui_prop_value}'"
    return f"'{maui_prop_value}' /* TODO: Check value/binding */"


def parse_xaml_node_to_jsx(node, indent_level, style_collector, event_handler_collector):
    maui_tag_full = node.tag
    maui_tag = maui_tag_full.split('}')[-1] if '}' in maui_tag_full else maui_tag_full
    rn_tag = MAUI_TO_RN_ELEMENT_MAP.get(maui_tag, f"Unsupported_{maui_tag}")

    jsx_props = []
    inline_styles = {}

    for key, value in node.attrib.items():
        # Skip XAML specific attributes
        if key.startswith('{http://schemas.microsoft.com/winfx/2009/xaml}') or \
           key.startswith('xmlns') or \
           key == "x:Name": # x:Name could be used for refs, but complex
            continue

        rn_prop_key = convert_prop_name(maui_tag, key)
        rn_prop_value = convert_prop_value(key, value, rn_prop_key)

        if rn_prop_key.startswith("on") and callable(getattr(value, "lower", None)): # Event handler
            handler_name = value
            event_handler_collector.add(handler_name)
            jsx_props.append(f"{rn_prop_key}={{ {handler_name} }}")
        elif rn_prop_key in ["backgroundColor", "color", "fontSize", "padding", "margin", "height", "width", "flexDirection", "alignSelf", "gap"]:
            # Collect common style properties
            inline_styles[rn_prop_key] = rn_prop_value
        elif rn_prop_key == "children" and rn_tag == "Text": # Handled by node.text
            continue
        else:
            jsx_props.append(f"{rn_prop_key}={{{rn_prop_value}}}") # Wrap values in {} for JSX

    # Assign style
    style_name = None
    if inline_styles:
        # Create a unique name for this style object (very basic)
        style_name = f"{rn_tag.lower()}Style{len(style_collector) + 1}"
        style_collector[style_name] = inline_styles
        jsx_props.append(f"style={{styles.{style_name}}}")

    indent = "  " * indent_level
    jsx_string = f"{indent}<{rn_tag}"
    if jsx_props:
        jsx_string += " " + " ".join(jsx_props)

    children_jsx = []
    if node.text and node.text.strip():
        if rn_tag == "Text":
            children_jsx.append(f"{indent}  {node.text.strip()}")
        else: # Text content in non-Text components might be an error or need special handling
            children_jsx.append(f"{indent}  {{/* TODO: Text content in {rn_tag}: '{node.text.strip()}' */}}")


    for child_node in node:
        child_jsx = parse_xaml_node_to_jsx(child_node, indent_level + 1, style_collector, event_handler_collector)
        children_jsx.append(child_jsx)

    if children_jsx:
        jsx_string += ">\n"
        jsx_string += "\n".join(children_jsx)
        jsx_string += f"\n{indent}</{rn_tag}>"
    else:
        jsx_string += " />"

    return jsx_string

def convert_maui_cs_to_rn(maui_cs_filepath):
    # Try to find the corresponding .xaml file
    xaml_filepath = maui_cs_filepath.replace(".xaml.cs", ".xaml").replace(".cs", ".xaml")
    if not os.path.exists(xaml_filepath):
        return (f"// Error: Corresponding XAML file not found: {xaml_filepath}\n"
                "// This script primarily converts UI defined in XAML.")

    try:
        with open(xaml_filepath, 'r', encoding='utf-8') as f:
            xaml_content = f.read()
    except Exception as e:
        return f"// Error reading XAML file {xaml_filepath}: {e}"

    try:
        # Remove comments to avoid parsing issues
        xaml_content = re.sub(r'<!--.*?-->', '', xaml_content, flags=re.DOTALL)
        root = ET.fromstring(xaml_content)
    except ET.ParseError as e:
        return f"// Error parsing XAML: {e}\n// XAML content (first 500 chars):\n// {xaml_content[:500]}"

    component_name = sanitize_component_name(maui_cs_filepath)
    styles_collected = {}
    event_handlers_collected = set()

    # The actual root for JSX is often the first child of ContentPage/ContentView
    # or the ContentPage/ContentView itself if it's a simple container.
    # For this example, we'll convert the root XAML element.
    jsx_body = parse_xaml_node_to_jsx(root, 2, styles_collected, event_handlers_collected) # Start indent 2

    # --- Generate React Native file content ---
    imports = set(["React from 'react'"])
    rn_elements_used = re.findall(r"<([A-Z][a-zA-Z0-9_]+)", jsx_body)
    native_imports = set()
    for el in rn_elements_used:
        if el not in ["Unsupported", "View", "Text", "Button", "TextInput", "ScrollView", "Image"]: # Common ones
            native_imports.add(el) # Add others that might be custom or less common
    if "View" in rn_elements_used or "ContentPage" in rn_elements_used or "ContentView" in rn_elements_used or "StackLayout" in rn_elements_used or "Grid" in rn_elements_used:
        native_imports.add("View")
    if "Text" in rn_elements_used or "Label" in rn_elements_used: native_imports.add("Text")
    if "Button" in rn_elements_used: native_imports.add("Button")
    if "TextInput" in rn_elements_used or "Entry" in rn_elements_used: native_imports.add("TextInput")
    if "ScrollView" in rn_elements_used: native_imports.add("ScrollView")
    if "Image" in rn_elements_used: native_imports.add("Image")

    if styles_collected:
        native_imports.add("StyleSheet")
    if "ContentPage" in rn_elements_used: # A common root
        native_imports.add("SafeAreaView") # Often a good replacement for Page

    import_statements = "import React from 'react';\n"
    if native_imports:
        import_statements += f"import {{ {', '.join(sorted(list(native_imports)))} }} from 'react-native';\n"

    handler_declarations = "\n  // --- TODO: Implement Event Handlers ---\n"
    for handler in sorted(list(event_handlers_collected)):
        handler_declarations += f"  const {handler} = () => {{\n"
        handler_declarations += f"    console.warn('{handler} not implemented');\n"
        handler_declarations += "  };\n"

    style_sheet_code = ""
    if styles_collected:
        style_sheet_code = "\nconst styles = StyleSheet.create({\n"
        for name, props_dict in styles_collected.items():
            style_sheet_code += f"  {name}: {{\n"
            for prop, val in props_dict.items():
                style_sheet_code += f"    {prop}: {val},\n" # Assumes val is already formatted (e.g. number or 'string')
            style_sheet_code += "  },\n"
        style_sheet_code += "});\n"

    # Determine the top-level returned component. If ContentPage was used, SafeAreaView is a good bet.
    # This is a heuristic.
    outer_component_open = "  <>"
    outer_component_close = "  </>"
    if "ContentPage" in rn_elements_used and "SafeAreaView" in native_imports:
        outer_component_open = "  <SafeAreaView style={{flex: 1}}>" # Basic full screen style
        outer_component_close = "  </SafeAreaView>"


    rn_code = f"""{import_statements}
// Original MAUI file: {os.path.basename(maui_cs_filepath)}
// Generated on: {T3_CHAT_CURRENT_DATE_TIME}
// WARNING: This is an automated conversion and will require significant manual review and modification.

const {component_name} = () => {{
{handler_declarations if event_handlers_collected else "  // No event handlers extracted from XAML."}
  return (
{outer_component_open}
{jsx_body}
{outer_component_close}
  );
}};
{style_sheet_code}
export default {component_name};
"""
    return rn_code

# --- Example Usage (requires dummy files to exist) ---
if __name__ == "__main__":
    # Create dummy MAUI files for testing
    # 1. TestPage.xaml
    test_xaml_content = """<?xml version="1.0" encoding="utf-8" ?>
<ContentPage xmlns="http://schemas.microsoft.com/dotnet/2021/maui"
             xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml"
             x:Class="MyMauiApp.TestPage"
             Title="Test Page">
    <StackLayout Orientation="Vertical" Spacing="10" Padding="20">
        <Label Text="Hello, MAUI!" FontSize="24" TextColor="Blue" HorizontalOptions="Center" />
        <Entry x:Name="myEntry" Placeholder="Enter text here" TextChanged="OnMyEntryTextChanged" />
        <Button Text="Click Me" Clicked="OnTestButtonClicked" BackgroundColor="LightGray" />
        <Image Source="dotnet_bot.png" HeightRequest="50" WidthRequest="50" />
    </StackLayout>
</ContentPage>
"""
    with open("TestPage.xaml", "w", encoding="utf-8") as f:
        f.write(test_xaml_content)

    # 2. TestPage.xaml.cs
    test_cs_content = """
namespace MyMauiApp
{
    public partial class TestPage : ContentPage
    {
        public TestPage()
        {
            InitializeComponent();
        }

        private void OnTestButtonClicked(object sender, System.EventArgs e)
        {
            // C# logic would be here
            System.Console.WriteLine("Test Button Clicked in MAUI");
        }

        private void OnMyEntryTextChanged(object sender, TextChangedEventArgs e)
        {
            // C# logic
            System.Console.WriteLine($"Text changed to: {e.NewTextValue}");
        }
    }
}
"""
    with open("TestPage.xaml.cs", "w", encoding="utf-8") as f:
        f.write(test_cs_content)

    print(f"--- Attempting to convert TestPage.xaml.cs ---")
    # Note: The T3_CHAT_CURRENT_DATE_TIME placeholder would be filled by the environment.
    # For local testing, you can replace it or define it.
    T3_CHAT_CURRENT_DATE_TIME = "LOCAL_TEST_TIME"
    react_native_output = convert_maui_cs_to_rn("TestPage.xaml.cs")

    print("\n--- Generated React Native Code (TestPage.tsx) ---")
    print(react_native_output)

    # Save the output
    with open("TestPage.tsx", "w", encoding="utf-8") as f:
        f.write(react_native_output)
    print("\nSaved to TestPage.tsx")

    # Clean up dummy files
    os.remove("TestPage.xaml")
    os.remove("TestPage.xaml.cs")
    # os.remove("TestPage.tsx") # Keep it to inspect
