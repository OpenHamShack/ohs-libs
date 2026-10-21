package com.openhamshack.kind.icom

// A comment with "Quoted text" and Text("Not code") is never a finding.
/* Block comments /* nest */ in Kotlin, so this "string" is still a comment. */

@Composable
fun IcomScreen(vm: IcomViewModel, n: Int) {
    Text("Tune")  // expect-finding
    Text(text = "Transmit")  // expect-finding
    Button(onClick = { vm.ptt() }) { Text("PTT on ${vm.freq}") }  // expect-finding
    OutlinedTextField(value = vm.call, onValueChange = {}, label = { Text("Callsign") })  // expect-finding
    Icon(Icons.Default.Mic, contentDescription = "Microphone")  // expect-finding
    TopAppBar(title = if (vm.tx) "On air" else "Idle")  // expect-finding (two literals)
    Text("$n stations heard")  // expect-finding
    Text("Frequency: " + vm.freqText)  // expect-finding
    val raw = """Raw "quoted" text"""
    Text("""A raw ${vm.mode} label""")  // expect-finding
}

fun legacy(view: TextView, ctx: Context) {
    view.setText("Ready")  // expect-finding
    view.text = "Waiting for the radio"  // expect-finding
    Toast.makeText(ctx, "Saved", Toast.LENGTH_SHORT).show()  // expect-finding
    NotificationCompat.Builder(ctx, "tx")
        .setContentTitle("Transmitting")  // expect-finding
        .setContentText(
            "PTT held"  // expect-finding
        )
}
