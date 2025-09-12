import re
import os
from bs4 import BeautifulSoup, Comment, Tag, NavigableString
import argparse

# --- Configuration ---
# Basic mapping of ASP.NET controls to React/HTML elements
ASP_TO_REACT_MAP = {
    "asp:label": {"tag": "span", "text_attr": "Text", "children_from_text": True},
    "asp:textbox": {
        "tag": "input",
        "props": {"type": "text"},
        "value_attr": "Text", # Attribute in ASPX that might hold initial value
        "stateful": True,
    },
    "asp:button": {"tag": "button", "text_attr": "Text", "children_from_text": True},
    "asp:literal": {"tag": "span", "comment": "Content for Literal usually set in code-behind"},
    "asp:hyperlink": {"tag": "a", "text_attr": "Text", "props_map": {"navigaturl": "href"}},
    "asp:panel": {"tag": "div"},
    "asp:image": {"tag": "img", "props_map": {"imageurl": "src", "alternatetext": "alt"}},
    "asp:checkbox": {
        "tag": "input",
        "props": {"type": "checkbox"},
        "value_attr": "Checked", # Attribute in ASPX for initial checked state
        "stateful": True,
        "label_attr": "Text" # If Text is present, wrap with a label
    },
}

JSX_ATTR_MAP = {
    "class": "className",
    "for": "htmlFor",
    "maxlength": "maxLength",
    "readonly": "readOnly",
    "tabindex": "tabIndex",
    "accesskey": "accessKey",
    "contenteditable": "contentEditable",
    "contextmenu": "contextMenu",
    "spellcheck": "spellCheck",
    # Add more as needed
}

ASP_EVENT_MAP = {
    "onclick": "onClick",
    "oncommand": "onCommand",
    "ontextchanged": "onChange",
    "oncheckedchanged": "onChange",
    # Add more ASP.NET event names and their typical React counterparts
}

# --- Helper Functions ---
def to_camel_case(name):
    if not name: return ""
    if '_' in name: parts = name.split('_'); return parts[0].lower() + "".join(p.title() for p in parts[1:])
    if '-' in name: parts = name.split('-'); return parts[0].lower() + "".join(p.title() for p in parts[1:])
    return name[0].lower() + name[1:]

def to_pascal_case(name):
    if not name: return ""
    name = name.replace('-', '_') # Treat dashes like underscores for PascalCase
    if '_' in name: parts = name.split('_'); return "".join(p.title() for p in parts)
    return name[0].upper() + name[1:]

def get_control_id(tag):
    return tag.get("id", None)

# --- ASPX Parsing and JSX Conversion ---
def convert_attributes_to_jsx_props(attrs, control_id, asp_control_config=None):
    props = {}
    event_handlers_to_stub = {}

    for key, value in attrs.items():
        original_key = key
        key_lower = key.lower()

        if original_key.lower() == "style" and isinstance(value, str):
            jsx_key = "style"
            style_obj_props = {}
            try:
                for style_item in value.split(';'):
                    if ':' in style_item:
                        k, v = style_item.split(':', 1)
                        k_norm = k.strip()
                        style_jsx_key = to_camel_case(k_norm) # CSS kebab-case to JS camelCase
                        style_obj_props[style_jsx_key] = v.strip()
                style_obj_str_parts = [f"{sk}: '{sv}'" for sk, sv in style_obj_props.items()]
                props[jsx_key] = f"{{{{{', '.join(style_obj_str_parts)}}}}}"
            except Exception:
                 props[jsx_key] = f"{{/* Style Error: Could not parse '{value}' */}}"
            continue
        elif asp_control_config and "props_map" in asp_control_config and original_key.lower() in asp_control_config["props_map"]:
            jsx_key = asp_control_config["props_map"][original_key.lower()]
        elif key_lower in JSX_ATTR_MAP:
            jsx_key = JSX_ATTR_MAP[key_lower]
        elif key_lower.startswith("on") and key_lower in ASP_EVENT_MAP:
            jsx_key = ASP_EVENT_MAP[key_lower]
            handler_func_name = f"handle{to_pascal_case(value)}"
            props[jsx_key] = f"{{{handler_func_name}}}"
            event_handlers_to_stub[jsx_key] = value
            continue
        else:
            jsx_key = to_camel_case(original_key) # Default to camelCase for unknown attributes

        if isinstance(value, str) and value.lower() in ['true', 'false']:
            props[jsx_key] = "{"+value.lower()+"}"
        elif isinstance(value, list):
             props[jsx_key] = " ".join(value)
        else:
            if "<%#" in value or "<%=" in value or "<%$" in value:
                props[jsx_key] = f"{{/* ASP.NET Binding: {value} */}}"
            else:
                props[jsx_key] = str(value)
    return props, event_handlers_to_stub

def convert_node_to_jsx_recursive(node, indent_level=0, server_controls_info=None):
    if server_controls_info is None:
        server_controls_info = {"ids": set(), "stateful_controls": [], "event_handlers": {}}
    indent = "  " * indent_level
    jsx_string = ""

    if isinstance(node, Comment): return f"{indent}{{/* {node.string.strip()} */}}\n"
    if isinstance(node, NavigableString):
        text = node.string.strip()
        if text: return f"{indent}{text.replace('{', '{{').replace('}', '}}')}\n"
        return ""
    if not isinstance(node, Tag): return ""

    tag_name_lower = node.name.lower()
    attrs = node.attrs
    control_id = get_control_id(node)

    if tag_name_lower.startswith("asp:"):
        control_type = tag_name_lower
        if control_id: server_controls_info["ids"].add(control_id)
        config = ASP_TO_REACT_MAP.get(control_type)

        if config:
            jsx_tag = config["tag"]
            current_props, current_event_handlers = convert_attributes_to_jsx_props(
                {**(config.get("props", {})), **attrs}, control_id, config
            )
            for event_prop, cs_handler in current_event_handlers.items():
                if control_id:
                    js_handler_name = f"handle{to_pascal_case(cs_handler)}"
                    server_controls_info["event_handlers"][js_handler_name] = {
                        "cs_method": cs_handler, "control_id": control_id
                    }

            if config.get("stateful") and control_id:
                state_var_name = to_camel_case(control_id)
                setter_name = f"set{to_pascal_case(control_id)}"
                control_type_for_state = "string"
                initial_value_from_attr = attrs.get(config.get("value_attr", ""), "")
                if control_type == "asp:checkbox":
                    control_type_for_state = "boolean"
                    current_props["checked"] = f"{{{state_var_name}}}"
                    current_props["onChange"] = f"{{(e) => {setter_name}(e.target.checked)}}"
                else: # textbox etc.
                    current_props["value"] = f"{{{state_var_name}}}"
                    current_props["onChange"] = f"{{(e) => {setter_name}(e.target.value)}}"

                server_controls_info["stateful_controls"].append({
                    "id": control_id, "name": state_var_name, "setter": setter_name,
                    "type": control_type_for_state, "initial_value_attr_name": config.get("value_attr"),
                    "initial_value_from_aspx": initial_value_from_attr
                })


            text_content = ""
            if config.get("children_from_text"):
                text_attr_name = config.get("text_attr", "Text")
                text_value = next((attr_val for attr_key, attr_val in attrs.items() if attr_key.lower() == text_attr_name.lower()), None)
                if text_value:
                    text_content = text_value.replace("{", "{{").replace("}", "}}")
                    # Remove the original ASP.NET text attribute if mapped to children
                    prop_to_remove = to_camel_case(text_attr_name)
                    if prop_to_remove in current_props: del current_props[prop_to_remove]
                elif node.string: text_content = node.string.strip().replace("{", "{{").replace("}", "}}")

            if control_type == "asp:checkbox" and config.get("label_attr"):
                label_text_attr_name = config.get("label_attr")
                label_text_val = next((v for k,v in attrs.items() if k.lower() == label_text_attr_name.lower()), None)
                if label_text_val:
                    prop_to_remove = to_camel_case(label_text_attr_name)
                    if prop_to_remove in current_props: del current_props[prop_to_remove]
                    props_str_checkbox = " ".join(f'{k}="{v}"' if not (str(v).startswith("{{") and str(v).endswith("}}")) else f'{k}={v}' for k, v in current_props.items())
                    jsx_string += f"{indent}<label>\n"
                    jsx_string += f"{indent}  <{jsx_tag} {props_str_checkbox} />\n"
                    jsx_string += f"{indent}  {label_text_val.replace('{', '{{').replace('}', '}}')}\n"
                    jsx_string += f"{indent}</label>\n"
                    return jsx_string

            props_str = " ".join(f'{k}="{v}"' if not (str(v).startswith("{{") and str(v).endswith("}}")) else f'{k}={v}' for k, v in current_props.items())
            if config.get("comment"): jsx_string += f"{indent}{{/* TODO: {config['comment']} for ID: {control_id or 'N/A'} */}}\n"

            has_children_tags = any(isinstance(child, Tag) for child in node.children)
            if text_content or has_children_tags or (not text_content and node.contents and not config.get("children_from_text")):
                jsx_string += f"{indent}<{jsx_tag} {props_str}>\n"
                if text_content: jsx_string += f"{indent}  {text_content}\n"
                for child in node.children: jsx_string += convert_node_to_jsx_recursive(child, indent_level + 1, server_controls_info)
                jsx_string += f"{indent}</{jsx_tag}>\n"
            else:
                jsx_string += f"{indent}<{jsx_tag} {props_str} />\n"
        else:
            jsx_string += f"{indent}{{/* TODO: Convert Unmapped ASP.NET Control <{node.name} ID=\"{control_id or 'N/A'}\" runat=\"server\"> */}}\n"
            for child in node.children: jsx_string += convert_node_to_jsx_recursive(child, indent_level + 1, server_controls_info)
            if any(isinstance(child, Tag) for child in node.children): jsx_string += f"{indent}{{/* End of children for {node.name} */}}\n"
    else: # Standard HTML tag
        jsx_tag = tag_name_lower
        current_props, _ = convert_attributes_to_jsx_props(attrs, control_id) # Ignoring event handlers for plain HTML for now
        props_str = " ".join(f'{k}="{v}"' if not (str(v).startswith("{{") and str(v).endswith("}}")) else f'{k}={v}' for k, v in current_props.items())
        
        self_closing_html_tags = ["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]
        if not node.contents and jsx_tag in self_closing_html_tags:
            jsx_string += f"{indent}<{jsx_tag} {props_str} />\n"
        else:
            jsx_string += f"{indent}<{jsx_tag} {props_str}>\n"
            for child in node.children: jsx_string += convert_node_to_jsx_recursive(child, indent_level + 1, server_controls_info)
            jsx_string += f"{indent}</{jsx_tag}>\n"
    return jsx_string

def parse_aspx_to_jsx(aspx_content):
    soup = BeautifulSoup(aspx_content, 'html.parser')
    jsx_body = ""
    server_controls_info = {"ids": set(), "stateful_controls": [], "event_handlers": {}}
    form_tag = soup.find("form")
    content_root = form_tag if form_tag else soup.body if soup.body else soup
    if content_root:
        for child in content_root.children: jsx_body += convert_node_to_jsx_recursive(child, 1, server_controls_info)
    else:
        for child in soup.children: jsx_body += convert_node_to_jsx_recursive(child, 1, server_controls_info)
    return jsx_body.strip(), server_controls_info

# --- C# Code-behind Parsing (Rudimentary) ---
def extract_cs_method_body(cs_content, method_name_pattern):
    method_pattern_str = rf"(?:public|protected|private|internal)?\s*(?:static|virtual|override|async)?\s*\w+(?:<[^>]+>)?\s+{method_name_pattern}\s*\([^)]*\)\s*\{{"
    match = re.search(method_pattern_str, cs_content, re.MULTILINE)
    if not match: return None
    start_index = match.end() -1
    brace_level = 0; body_start_index = -1; body_end_index = -1
    for i in range(start_index, len(cs_content)):
        if cs_content[i] == '{':
            if brace_level == 0: body_start_index = i + 1
            brace_level += 1
        elif cs_content[i] == '}':
            brace_level -= 1
            if brace_level == 0 and body_start_index != -1: body_end_index = i; break
    if body_start_index != -1 and body_end_index != -1:
        return cs_content[body_start_index:body_end_index].strip()
    return None

def parse_cs_file(cs_content, server_controls_info):
    page_load_content_comment = ""
    event_handler_functions_ts = []
    page_load_body = extract_cs_method_body(cs_content, "Page_Load")
    if page_load_body: page_load_content_comment = f"// Original Page_Load content:\n/*\n{page_load_body}\n*/"

    for js_name, handler_info in server_controls_info.get("event_handlers", {}).items():
        cs_method_name = handler_info["cs_method"]
        control_id = handler_info["control_id"]
        cs_method_body = extract_cs_method_body(cs_content, cs_method_name)
        ts_func = f"  const {js_name} = () => {{\n"
        ts_func += f"    // TODO: Implement logic for {cs_method_name} (control: {control_id})\n"
        if cs_method_body: ts_func += f"    // Original C#:\n    /*\n{cs_method_body.replace('/*', '//').replace('*/', '//')}\n    */\n"
        else: ts_func += f"    // C# method {cs_method_name} body not found/parsed.\n"
        ts_func +=  f"    console.log('{js_name} triggered for {control_id}');\n  }};\n"
        event_handler_functions_ts.append(ts_func)
    return page_load_content_comment, "\n".join(event_handler_functions_ts)

# --- React Component Generation ---
def generate_react_component(component_name, jsx_body, server_controls_info, page_load_cs_comment, event_handlers_ts_code):
    imports = ["import React, { useState, useEffect } from 'react';"]
    states_ts = []
    for ctrl in server_controls_info.get("stateful_controls", []):
        initial_value_str = '""' # Default for string
        if ctrl["type"] == "boolean":
            initial_value_str = "false"
            if ctrl["initial_value_from_aspx"].lower() == "true": initial_value_str = "true"
        else: # string
            if ctrl["initial_value_from_aspx"]:
                # Escape for string literal
                escaped_initial_val = ctrl["initial_value_from_aspx"].replace('\\', '\\\\').replace("'", "\\'")
                initial_value_str = f"'{escaped_initial_val}'"

        states_ts.append(f"  const [{ctrl['name']}, {ctrl['setter']}] = useState({initial_value_str});")

    component_code = f"""
{'\n'.join(imports)}

// TODO: Define props interface if this component needs to accept props
// interface {component_name}Props {{
//   someProp?: string;
// }}

const {component_name}: React.FC/*<YourPropsInterfaceHere>*/ = (/*props*/) => {{
{"" if not states_ts else "\n".join(states_ts)}

  useEffect(() => {{
    // TODO: Equivalent of Page_Load if necessary.
    // Consider fetching data here if Page_Load was used for initial data retrieval.
    {page_load_cs_comment.replace("\n", "\n    ") if page_load_cs_comment else "// No Page_Load content found or extracted."}
  }}, []); // Empty dependency array means this runs once on mount

{event_handlers_ts_code if event_handlers_ts_code else "  // No event handlers extracted or defined."}

  return (
    <>
      {jsx_body if jsx_body else "{/* TODO: Add JSX content */}"}
    </>
  );
}};

export default {component_name};
"""
    return component_code.strip()

# --- Main Script Logic ---
def main():
    parser = argparse.ArgumentParser(description="Convert ASP.NET WebForms files to a React TypeScript component.")
    parser.add_argument("aspx_file", help="Path to the .aspx file.")
    parser.add_argument("--cs_file", help="Path to the .aspx.cs code-behind file (optional).")
    args = parser.parse_args()

    if not os.path.exists(args.aspx_file): print(f"Error: ASPX file not found: {args.aspx_file}"); return
    base_name = os.path.splitext(os.path.basename(args.aspx_file))[0]
    react_component_name = to_pascal_case(base_name.replace('.', '_').replace('-', '_'))

    print(f"Processing {args.aspx_file} into {react_component_name}.g.tsx...")
    try:
        with open(args.aspx_file, 'r', encoding='utf-8-sig') as f: aspx_content = f.read() # utf-8-sig for BOM
    except Exception as e: print(f"Error reading ASPX file: {e}"); return

    jsx_body, server_controls_info = parse_aspx_to_jsx(aspx_content)
    
    page_load_comment = "// .cs file not processed or Page_Load not found."
    event_handlers_code = ""
    # Generate empty stubs if no CS file, based on ASPX findings
    temp_event_handlers = []
    for js_name, handler_info in server_controls_info.get("event_handlers", {}).items():
            temp_event_handlers.append(f"  const {js_name} = () => {{ console.log('{js_name} triggered (from ASPX: {handler_info['cs_method']} for {handler_info['control_id']}), C# body not available/parsed.'); }};")
    event_handlers_code = "\n".join(temp_event_handlers)


    if args.cs_file:
        if os.path.exists(args.cs_file):
            print(f"Processing code-behind {args.cs_file}...")
            try:
                with open(args.cs_file, 'r', encoding='utf-8-sig') as f: cs_content = f.read()
                page_load_comment, event_handlers_code = parse_cs_file(cs_content, server_controls_info)
            except Exception as e: print(f"Warning: Error reading/parsing CS file: {e}")
        else: print(f"Warning: CS file not found: {args.cs_file}")

    react_output_code = generate_react_component(
        react_component_name, jsx_body, server_controls_info, page_load_comment, event_handlers_code
    )
    output_filename = f"{base_name}.g.tsx"
    try:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write(react_output_code)
        print(f"\nSuccessfully generated React component: {output_filename}")
        print("\n" + "="*30 + " IMPORTANT NOTES " + "="*30)
        print("- This is a VERY BASIC conversion. Significant manual review and refactoring are REQUIRED.")
        print("- Data binding (<%# ... %>) is NOT converted. Implement API calls and state management.")
        print("- Complex server controls (GridView, Repeater, UpdatePanel, etc.) are NOT converted.")
        print("- Server-side logic needs to be manually ported or exposed via APIs.")
        print("- ViewState, Session, Master Pages, User Controls are NOT handled.")
        print("- Review all 'TODO:' comments and implement missing logic.")
        print("="*77)
    except Exception as e: print(f"Error writing output file: {e}")

if __name__ == '__main__':
    main()
