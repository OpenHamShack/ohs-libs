package com.openhamshack.kind.icom

@Composable
fun IcomScreen(vm: IcomViewModel, n: Int) {
    Text(stringResource(R.string.tune))
    Text(text = stringResource(R.string.transmit))
    Text(pluralStringResource(R.plurals.stations_heard, n, n))
    Text(vm.callsign)  // data
    Text("—")
    Text("${vm.freq}")  // no literal letters
    Icon(Icons.Default.Mic, contentDescription = null)
    Log.d("IcomScreen", "Transmit pressed")  // log, not a sink
    val title = "Not shown"  // a declaration, not a title = argument
    val route = mapOf("title" to "settings_screen")
    if (vm.mode == "USB") {}  // comparison, not assignment
}
