# Name ideas

Argus stays **Argus** for now. This is the longer list to come back to. A name can be changed any time:
set `name = "..."` in `~/.argus/config.toml` and run `argusctl restart`. The wake word changes with it.

**What makes a good wake word:** two or three syllables, sounds that are rare in everyday speech, and
not a common word. "Argus" is good. "Nova" is okay but close to "no, va…". "Echo" and "Iris" are taken
by other assistants or are common.

## Full forms for A.R.G.U.S.

### Personal, like "AIFS"
- **A**lways-**R**eady **G**uardian **U**nit for **S**andeep
- **A**dvanced **R**easoning & **G**uidance **U**nit for **S**andeep
- **A**daptive **R**easoning & **G**eneral **U**tility for **S**andeep
- **A**utonomous **R**esponsive **G**uardian **U**nit for **S**andeep
- **A**rtificial **R**easoning & **G**uidance, **U**niquely **S**andeep's
- **A**ssistant **R**unning **G**enuinely **U**seful **S**ervices (for Sandeep)

### From the myth (Argus Panoptes, the hundred-eyed watchman who never slept)
- **A**ll-seeing **R**esponsive **G**uardian, **U**nblinking & **S**ecure
- **A**lways **R**emembering, **G**uarding, **U**nderstanding, **S**erving
- **A**ttentive **R**easoning **G**uardian with **U**nblinking **S**ight
- **A** **R**estless **G**uardian **U**nder **S**ilence (it listens, but never records)
- **A**ll **R**ound **G**uardian of **U**ser **S**ecurity

### JARVIS-style ("Just A Rather Very Intelligent System")
- **A** **R**ather **G**ifted **U**niversal **S**ystem
- **A**bsolutely **R**eliable, **G**enerally **U**nflappable **S**idekick
- **A** **R**emarkably **G**ood **U**tility **S**ystem
- **A**lways **R**ight, **G**enerally **U**nderstated **S**ystem (aspirational)

### Systems-sounding
- **A**mbient **R**easoning & **G**enerative **U**ser **S**ystem
- **A**utonomous **R**eal-time **G**uardian & **U**tility **S**ystem
- **A**daptive **R**eal-time **G**eneral-purpose **U**niversal **S**ystem
- **A**gentic **R**easoning & **G**uidance **U**nified **S**ystem

## Other names, if Argus ever gets a sibling (or a rename)

### Watchers and guardians from mythology
| Name | Story | As a wake word |
|---|---|---|
| **Heimdall** | Norse god who sees and hears everything, guarding the rainbow bridge | Strong and rare; long-ish |
| **Janus** | Roman god of doorways, looking both ways | Short, clear |
| **Vigil** | Latin for "watchful" | Clear; a real word but rarely said |
| **Talos** | Bronze guardian of Crete | Rare, crisp |
| **Mimir** | Norse well of wisdom, an advisor's head that answers questions | Distinctive |

### Indian and Sanskrit
| Name | Meaning | As a wake word |
|---|---|---|
| **Sanjaya** | Saw the whole Kurukshetra war from afar with divine sight and narrated it to the blind king: an assistant that sees for you | Rare in English speech: excellent |
| **Drishti** | Vision, sight | Distinctive |
| **Garuda** | Vishnu's swift eagle mount, a messenger and protector | Rare, punchy |
| **Kavach** | Armour, shield: fits the security focus | Short, distinctive |
| **Vidura** | The wisest advisor in the Mahabharata, known for honest counsel | Rare |
| **Nandi** | Shiva's devoted gatekeeper | Short; can be misheard as "Andy" |
| **Rakshak** | Protector | Clear |

### Modern and sci-fi
| Name | Feel | As a wake word |
|---|---|---|
| **Orion** | The hunter; bright and easy to say | Good, but used by some products |
| **Atlas** | Holds the world up; dependable | Clear; also a common word in maps |
| **Vega** | Bright star; short and quick | Good |
| **Halo** | A protective ring | Easily confused with "hello" |
| **Onyx** | Calm, dark and solid | Distinctive |
| **Sable** | Smooth and quiet | Rare |

## How to choose

1. Say it ten times in a normal voice; it should feel natural to start a sentence with.
2. Run `argusctl enroll` after a rename. It shows how the speech recogniser hears the name in your voice,
   and learns your pronunciation.
3. Avoid names of people around you and of anything you often say near the Mac.
