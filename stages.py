"""Paper-aligned classical stages. Positions are zero-based original indices."""
import secrets


def parse_key(text, maximum=200000):
    if not isinstance(text, str):
        raise ValueError("Expected UTF-8 bit text")
    bits = "".join(text.lstrip("\ufeff").split())
    if not bits or len(bits) > maximum or set(bits) - {"0", "1"}:
        raise ValueError(f"Use 1..{maximum} binary digits, with optional whitespace; no headers or commas")
    return bits


def pairs_for(bits):
    # Match the paper's sequential sampling, including unbiased first orientation.
    groups = {"0": [i for i,b in enumerate(bits) if b == "0"],
              "1": [i for i,b in enumerate(bits) if b == "1"]}
    rng = secrets.SystemRandom()
    for values in groups.values():
        rng.shuffle(values)
    pairs = []
    while groups["0"] and groups["1"]:
        first = "0" if secrets.randbelow(len(groups["0"])+len(groups["1"])) < len(groups["0"]) else "1"
        pairs.append([groups[first].pop(), groups["1" if first == "0" else "0"].pop()])
    return pairs


def check_pairs(bits, pairs):
    if not isinstance(pairs, list) or len(pairs) > len(bits)//2:
        raise ValueError("Invalid pair list")
    seen, flags = set(), {}
    for number, pair in enumerate(pairs, 1):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Pairs must be two-element JSON arrays")
        for position in pair:
            if type(position) is not int or not 0 <= position < len(bits) or position in seen:
                raise ValueError("Out-of-bounds or duplicate pair position")
            seen.add(position)
        flags[f"P{number}"] = int(bits[pair[0]] == bits[pair[1]])
    return flags


def retain_pairs(bits, positions, pairs, flags):
    if not isinstance(flags, dict) or set(flags) != {f"P{i+1}" for i in range(len(pairs))}:
        raise ValueError("Acknowledgment does not identify exactly the proposed pairs")
    if any(type(flag) is not int or flag not in (0,1) for flag in flags.values()):
        raise ValueError("Pair flags must be 0=accepted or 1=rejected")
    selected = [pair[0] for i,pair in enumerate(pairs,1) if flags[f"P{i}"] == 0]
    return "".join(bits[i] for i in selected), [positions[i] for i in selected]


def validate_ad(length, size):
    if not length or type(size) is not int or not 2 <= size <= length or length % size:
        raise ValueError("AD requires a manually agreed integer b >= 2, b <= N and N % b == 0. AOPP checkpoint is unchanged; select another b.")


def mask_blocks(bits, size):
    validate_ad(len(bits),size)
    blocks=[]
    for start in range(0,len(bits),size):
        mask=secrets.randbits(1)
        blocks.append("".join(str(int(b)^mask) for b in bits[start:start+size]))
    return blocks


def check_blocks(bits, size, blocks):
    validate_ad(len(bits),size)
    if not isinstance(blocks,list) or len(blocks) != len(bits)//size:
        raise ValueError("Invalid masked block count")
    flags=[]
    for number,masked in enumerate(blocks):
        if not isinstance(masked,str) or len(masked)!=size or set(masked)-{"0","1"}:
            raise ValueError("Invalid masked block")
        block=bits[number*size:(number+1)*size]
        flags.append(int(len({a!=b for a,b in zip(block,masked)}) != 1))
    return flags


def retain_blocks(bits, positions, size, flags):
    validate_ad(len(bits),size)
    if not isinstance(flags,list) or len(flags)!=len(bits)//size or any(type(f) is not int or f not in (0,1) for f in flags):
        raise ValueError("Invalid AD acceptance flags")
    selected=[i*size for i,flag in enumerate(flags) if flag==0]
    return "".join(bits[i] for i in selected),[positions[i] for i in selected]
