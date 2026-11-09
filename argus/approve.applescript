-- Argus approval dialog. Arguments: message, "task" or "once", timeout in seconds.
on run argv
	activate
	set theMessage to item 1 of argv
	set theButtons to {"Deny", "Allow once"}
	if (item 2 of argv) is "task" then set theButtons to {"Deny", "Allow once", "Allow for this task"}
	try
		set answer to display dialog theMessage with title "Argus needs your OK" buttons theButtons default button "Deny" with icon caution giving up after ((item 3 of argv) as integer)
	on error
		return "Deny"
	end try
	if gave up of answer then return "Deny"
	return button returned of answer
end run
