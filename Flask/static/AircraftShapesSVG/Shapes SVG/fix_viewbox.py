"""
Fix SVG viewBox: parse each SVG file, compute the bounding box of all path
coordinates, and rewrite the viewBox to tightly wrap the artwork with a small
margin. This centers the visible content within the SVG coordinate system.
"""
import re, os, sys, glob
MARGIN_PERCENT = 0.05  # 5% padding around the artwork
def parse_path_coords(d_string):
    """Extract all numeric coordinate pairs from an SVG path 'd' attribute."""
    # Tokenize: split on commands and commas/whitespace
    tokens = re.findall(r'[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d_string)
    
    coords = []
    cmd = None
    nums = []
    cx, cy = 0.0, 0.0  # current point for relative commands
    sx, sy = 0.0, 0.0  # start of subpath
    
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            continue
        
        # Collect numbers
        try:
            val = float(t)
        except ValueError:
            i += 1
            continue
        
        if cmd in ('M', 'L', 'T'):
            # Absolute moveto/lineto: (x, y)
            if i + 1 < len(tokens):
                try:
                    x, y = val, float(tokens[i + 1])
                    coords.append((x, y))
                    cx, cy = x, y
                    if cmd == 'M':
                        sx, sy = x, y
                    i += 2
                    continue
                except (ValueError, IndexError):
                    pass
            i += 1
        elif cmd in ('m', 'l', 't'):
            # Relative moveto/lineto
            if i + 1 < len(tokens):
                try:
                    dx, dy = val, float(tokens[i + 1])
                    cx += dx
                    cy += dy
                    coords.append((cx, cy))
                    if cmd == 'm':
                        sx, sy = cx, cy
                    i += 2
                    continue
                except (ValueError, IndexError):
                    pass
            i += 1
        elif cmd == 'H':
            cx = val
            coords.append((cx, cy))
            i += 1
        elif cmd == 'h':
            cx += val
            coords.append((cx, cy))
            i += 1
        elif cmd == 'V':
            cy = val
            coords.append((cx, cy))
            i += 1
        elif cmd == 'v':
            cy += val
            coords.append((cx, cy))
            i += 1
        elif cmd == 'C':
            # Absolute cubic bezier: 6 numbers
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 6)]
                for k in range(0, 6, 2):
                    coords.append((nums[k], nums[k + 1]))
                cx, cy = nums[4], nums[5]
                i += 6
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'c':
            # Relative cubic bezier
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 6)]
                for k in range(0, 6, 2):
                    coords.append((cx + nums[k], cy + nums[k + 1]))
                cx += nums[4]
                cy += nums[5]
                i += 6
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'S':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 4)]
                for k in range(0, 4, 2):
                    coords.append((nums[k], nums[k + 1]))
                cx, cy = nums[2], nums[3]
                i += 4
            except (ValueError, IndexError):
                i += 1
        elif cmd == 's':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 4)]
                for k in range(0, 4, 2):
                    coords.append((cx + nums[k], cy + nums[k + 1]))
                cx += nums[2]
                cy += nums[3]
                i += 4
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'Q':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 4)]
                for k in range(0, 4, 2):
                    coords.append((nums[k], nums[k + 1]))
                cx, cy = nums[2], nums[3]
                i += 4
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'q':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 4)]
                for k in range(0, 4, 2):
                    coords.append((cx + nums[k], cy + nums[k + 1]))
                cx += nums[2]
                cy += nums[3]
                i += 4
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'A':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 7)]
                # endpoint
                coords.append((nums[5], nums[6]))
                cx, cy = nums[5], nums[6]
                i += 7
            except (ValueError, IndexError):
                i += 1
        elif cmd == 'a':
            try:
                nums = [val] + [float(tokens[i + j]) for j in range(1, 7)]
                cx += nums[5]
                cy += nums[6]
                coords.append((cx, cy))
                i += 7
            except (ValueError, IndexError):
                i += 1
        elif cmd in ('Z', 'z'):
            cx, cy = sx, sy
            i += 1
        else:
            i += 1
    
    return coords
def get_svg_bbox(svg_text):
    """Get bounding box of all paths in an SVG."""
    all_coords = []
    for match in re.finditer(r'd="([^"]*)"', svg_text):
        d = match.group(1)
        all_coords.extend(parse_path_coords(d))
    
    if not all_coords:
        return None
    
    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    return min(xs), min(ys), max(xs), max(ys)
def fix_svg_file(filepath):
    """Fix the viewBox of an SVG file to tightly wrap its content."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    bbox = get_svg_bbox(content)
    if bbox is None:
        print(f"  SKIP {os.path.basename(filepath)}: no paths found")
        return False
    
    min_x, min_y, max_x, max_y = bbox
    w = max_x - min_x
    h = max_y - min_y
    
    # Add margin
    margin_x = w * MARGIN_PERCENT
    margin_y = h * MARGIN_PERCENT
    
    new_vb_x = min_x - margin_x
    new_vb_y = min_y - margin_y
    new_vb_w = w + 2 * margin_x
    new_vb_h = h + 2 * margin_y
    
    old_vb_match = re.search(r'viewBox="([^"]*)"', content)
    old_vb = old_vb_match.group(1) if old_vb_match else "N/A"
    
    new_vb = f"{new_vb_x:.2f} {new_vb_y:.2f} {new_vb_w:.2f} {new_vb_h:.2f}"
    
    new_content = re.sub(r'viewBox="[^"]*"', f'viewBox="{new_vb}"', content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    center_x = min_x + w / 2
    center_y = min_y + h / 2
    print(f"  OK {os.path.basename(filepath):40s}  old: {old_vb:20s}  bbox: ({min_x:.1f},{min_y:.1f})-({max_x:.1f},{max_y:.1f})  center: ({center_x:.1f},{center_y:.1f})  new viewBox: {new_vb}")
    return True
def main():
    svg_dir = os.path.dirname(os.path.abspath(__file__))
    svg_files = glob.glob(os.path.join(svg_dir, '*.svg'))
    
    if not svg_files:
        print("No SVG files found!")
        return
    
    print(f"Found {len(svg_files)} SVG files. Fixing viewBoxes...\n")
    
    fixed = 0
    for f in sorted(svg_files):
        if fix_svg_file(f):
            fixed += 1
    
    print(f"\nDone! Fixed {fixed}/{len(svg_files)} files.")
if __name__ == '__main__':
    main()